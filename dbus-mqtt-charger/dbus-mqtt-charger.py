#!/usr/bin/env python
# dbus-mqtt-charger.py
#
# Bridges an ESPHome-controlled Huawei R4875G1 rectifier (or similar) to Venus
# OS as a com.victronenergy.charger service.
#
# Subscribes to multiple ESPHome MQTT state topics (one per sensor, ESPHome's
# default behaviour) and publishes the data on dbus. When DVCC is enabled in
# Venus and writes a new ChargeCurrent / ChargeVoltage setpoint to our
# /Link/* paths, we publish the value to the ESPHome `.../command` topic so the
# rectifier follows DVCC.
#
# Forked from mr-manuel/venus-os_dbus-mqtt-solar-charger.

from gi.repository import GLib  # pyright: ignore[reportMissingImports]
import platform
import logging
import sys
import os
from time import sleep, time
import configparser
import _thread

# import external packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext"))
import paho.mqtt.client as mqtt

# import Victron Energy packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))
from vedbus import VeDbusService  # noqa: E402
from ve_utils import get_vrm_portal_id  # noqa: E402


# ─────────────────────────── config ───────────────────────────
try:
    config_file = (os.path.dirname(os.path.realpath(__file__))) + "/config.ini"
    if os.path.exists(config_file):
        config = configparser.ConfigParser()
        config.read(config_file)
        if config["MQTT"]["broker_address"] == "IP_ADDR_OR_FQDN":
            print('ERROR:The "config.ini" still has default placeholder broker_address. Driver restarts in 60 s.')
            sleep(60)
            sys.exit()
    else:
        print('ERROR:The "' + config_file + '" is not found. Copy config.sample.ini to config.ini. Restarting in 60 s.')
        sleep(60)
        sys.exit()
except Exception:
    et, eo, etb = sys.exc_info()
    print(f"Exception loading config: {repr(eo)} of type {et} in {etb.tb_frame.f_code.co_filename} line #{etb.tb_lineno}")
    print("ERROR:Driver restarts in 60 s.")
    sleep(60)
    sys.exit()


# logging
_log_level_str = config["DEFAULT"].get("logging", "WARNING") if "DEFAULT" in config else "WARNING"
logging.basicConfig(level=getattr(logging, _log_level_str.upper(), logging.WARNING))


# timeout (seconds without any new MQTT data → exit so daemontools restarts us)
timeout = int(config["DEFAULT"].get("timeout", "60")) if "DEFAULT" in config else 60

# DVCC behaviour
_dvcc_section = "DVCC" if "DVCC" in config else "DEFAULT"
dvcc_enabled = config[_dvcc_section].get("dvcc_control_enabled", "0").strip() in ("1", "true", "True", "yes")
dvcc_max_current_safety = float(config[_dvcc_section].get("max_dc_current_safety", "75"))
dvcc_max_voltage_safety = float(config[_dvcc_section].get("max_dc_voltage_safety", "58"))
dvcc_min_voltage_safety = float(config[_dvcc_section].get("min_dc_voltage_safety", "48"))


# ─────────────────────────── globals ───────────────────────────
connected = 0          # MQTT broker connection state
last_changed = 0       # last time any subscribed state arrived
last_updated = 0       # last time dbus was synced from charger_dict
mqtt_client = None     # set in main()


# ─────────────────────────── formatting ───────────────────────────
def _a(p, v):
    return ("%.1f" % v) + "A" if v is not None else ""


def _n(p, v):
    return ("%i" % v) if v is not None else ""


def _s(p, v):
    return ("%s" % v) if v is not None else ""


def _v(p, v):
    return ("%.2f" % v) + "V" if v is not None else ""


def _w(p, v):
    return ("%i" % v) + "W" if v is not None else ""


def _t(p, v):
    return ("%.1f" % v) + "°C" if v is not None else ""


def _kwh(p, v):
    return ("%.3f" % v) + "kWh" if v is not None else ""


# ─────────────────────────── dbus path table ───────────────────────────
#
# Only paths needed for monitoring + DVCC. Skipping multi-output (/Dc/1, /Dc/2)
# because Huawei R4875G1 is single-output.
#
# /State numeric mapping for com.victronenergy.charger:
#   0=Off, 2=Fault, 3=Bulk, 4=Absorption, 5=Float, 6=Storage,
#   7=Equalize, 11=Power supply, 245=Wake-up, 252=External control
#
# /Mode: 1 = On, 4 = Off
charger_dict = {
    "/NrOfOutputs": {"value": 1, "textformat": _n},
    "/Dc/0/Voltage": {"value": None, "textformat": _v},
    "/Dc/0/Current": {"value": None, "textformat": _a},
    "/Dc/0/Temperature": {"value": None, "textformat": _t},
    "/Mode": {"value": 1, "textformat": _n},
    "/State": {"value": 0, "textformat": _n},
    "/ErrorCode": {"value": 0, "textformat": _n},
    "/Settings/ChargeCurrentLimit": {"value": None, "textformat": _a},
    # External control (DVCC). Venus writes target setpoints into these; our
    # _handlechangedvalue forwards them to the ESPHome command topic.
    "/Link/NetworkMode": {"value": 0, "textformat": _n},
    "/Link/ChargeCurrent": {"value": None, "textformat": _a},
    "/Link/ChargeVoltage": {"value": None, "textformat": _v},
}


# ─────────────────────────── topic → handler map ───────────────────────────
#
# Built at startup from config.ini. Each subscribed topic has:
#   handler  — callable(payload_str) → does whatever needs doing
#
# We use explicit per-topic subscriptions rather than a wildcard so we only
# touch what we care about and ignore the dozens of other ESPHome topics.

topic_handlers = {}


def _set_float(dbus_path, scale=1.0):
    def _h(payload):
        try:
            charger_dict[dbus_path]["value"] = float(payload) * scale
        except (ValueError, TypeError):
            logging.warning(f"Could not parse '{payload}' as float for {dbus_path}")
    return _h


def _handle_lwt(payload):
    # ESPHome publishes 'online' / 'offline' as last-will on its status topic.
    # We only flip /Connected via the dbusservice setter in _update(), so just
    # remember the desired state here.
    global _esphome_online
    _esphome_online = (payload.strip().lower() == "online")
    logging.info(f"ESPHome LWT: {'online' if _esphome_online else 'offline'}")


def _handle_power_state(payload):
    # ESPHome publishes 'ON' or 'OFF' for the charger power state.
    s = payload.strip().upper()
    if s in ("ON", "1", "TRUE"):
        charger_dict["/Mode"]["value"] = 1
    elif s in ("OFF", "0", "FALSE"):
        charger_dict["/Mode"]["value"] = 4


_esphome_online = True  # assume online until LWT tells us otherwise


def _derive_state():
    """
    Choose /State value based on /Mode, current and DVCC NetworkMode.
    Simplified: we don't try to detect Absorption vs Float — the Huawei is
    operated as a power supply with externally-set voltage/current, so the
    most accurate Victron state is 11 (Power supply) or 252 (External control)
    when DVCC is writing setpoints.
    """
    mode = charger_dict["/Mode"]["value"]
    if mode == 4:
        return 0  # Off
    current = charger_dict["/Dc/0/Current"]["value"] or 0
    network_mode = charger_dict["/Link/NetworkMode"]["value"] or 0
    if network_mode:
        return 252  # External control (DVCC actively writing)
    if current > 0.5:
        return 11  # Power supply (constant voltage source feeding battery)
    return 6        # Storage / idle


# ─────────────────────────── MQTT callbacks ───────────────────────────
def on_disconnect(client, userdata, flags, reason_code, properties):
    global connected
    logging.warning("MQTT: disconnected")
    while connected == 0:
        try:
            logging.warning(f"MQTT: reconnecting to {config['MQTT']['broker_address']}:{config['MQTT']['broker_port']}")
            client.connect(host=config["MQTT"]["broker_address"], port=int(config["MQTT"]["broker_port"]))
            connected = 1
        except Exception as err:
            logging.error(f"MQTT: reconnect failed: {err}")
            sleep(15)


def on_connect(client, userdata, flags, reason_code, properties):
    global connected
    if reason_code == 0:
        logging.info("MQTT: connected")
        connected = 1
        for topic in topic_handlers.keys():
            client.subscribe(topic)
            logging.info(f"MQTT: subscribed to {topic}")
    else:
        logging.error(f"MQTT: connect failed, reason_code={reason_code}")


def on_message(client, userdata, msg):
    global last_changed
    try:
        payload = msg.payload.decode(errors="replace") if isinstance(msg.payload, (bytes, bytearray)) else str(msg.payload)
        handler = topic_handlers.get(msg.topic)
        if handler is None:
            return
        handler(payload)
        last_changed = int(time())
    except Exception:
        et, eo, etb = sys.exc_info()
        logging.error(f"on_message error: {repr(eo)} of type {et} in {etb.tb_frame.f_code.co_filename} line #{etb.tb_lineno}")


# ─────────────────────────── dbus service ───────────────────────────
class DbusMqttChargerService:
    def __init__(self, servicename, deviceinstance, paths,
                 productname="MQTT Charger",
                 customname="MQTT Charger",
                 connection="MQTT Charger service",
                 cmd_topic_dc_current=None,
                 cmd_topic_dc_voltage=None):
        self._dbusservice = VeDbusService(servicename, register=False)
        self._paths = paths
        self._cmd_dc_current = cmd_topic_dc_current
        self._cmd_dc_voltage = cmd_topic_dc_voltage

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

        # Management
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path("/Mgmt/ProcessVersion",
                                   "1.0.0 dbus-mqtt-charger on Python " + platform.python_version())
        self._dbusservice.add_path("/Mgmt/Connection", connection)

        # Mandatory device info
        self._dbusservice.add_path("/DeviceInstance", deviceinstance)
        self._dbusservice.add_path("/ProductId", 0xFFFF)   # generic / unknown
        self._dbusservice.add_path("/ProductName", productname)
        self._dbusservice.add_path("/CustomName", customname)
        self._dbusservice.add_path("/FirmwareVersion", 100)
        self._dbusservice.add_path("/HardwareVersion", "1.0.0")
        # /Connected we manage explicitly from ESPHome LWT
        self._dbusservice.add_path("/Connected", 1)
        self._dbusservice.add_path("/Latency", None)

        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path,
                settings["value"],
                gettextcallback=settings["textformat"],
                writeable=True,
                onchangecallback=self._handlechangedvalue,
            )

        self._dbusservice.register()

        GLib.timeout_add(1000, self._update)

    def _update(self):
        global last_changed, last_updated
        now = int(time())

        if last_changed != last_updated:
            # Derive /State from current snapshot
            charger_dict["/State"]["value"] = _derive_state()

            # Push everything from charger_dict to dbus
            for path, data in charger_dict.items():
                try:
                    self._dbusservice[path] = data["value"]
                except Exception:
                    et, eo, etb = sys.exc_info()
                    logging.error(f"dbus set {path}={data['value']} failed: {repr(eo)} in {etb.tb_frame.f_code.co_filename}:{etb.tb_lineno}")

            # /Connected from ESPHome LWT
            self._dbusservice["/Connected"] = 1 if _esphome_online else 0

            v = charger_dict["/Dc/0/Voltage"]["value"] or 0
            i = charger_dict["/Dc/0/Current"]["value"] or 0
            logging.info("Charger: {:.2f} V × {:.2f} A = {:.0f} W (state={}, mode={})".format(
                v, i, v * i,
                charger_dict["/State"]["value"],
                charger_dict["/Mode"]["value"]))

            last_updated = last_changed

        # No data for too long → die so daemontools restarts us
        if timeout != 0 and (now - last_changed) > timeout:
            logging.error("No MQTT data for %i s — exiting." % timeout)
            sys.exit()

        # UpdateIndex tick
        idx = (self._dbusservice["/UpdateIndex"] + 1) & 0xFF
        self._dbusservice["/UpdateIndex"] = idx
        return True

    def _handlechangedvalue(self, path, value):
        """
        Venus DVCC engine writes to /Link/ChargeCurrent and /Link/ChargeVoltage
        when it wants to limit the charger. Forward those to ESPHome.
        Also accept manual writes to /Mode (1=On, 4=Off) and /Settings paths.
        """
        logging.debug(f"dbus set {path} = {value}")

        if not dvcc_enabled:
            return True

        if path == "/Link/ChargeCurrent" and self._cmd_dc_current:
            try:
                target = float(value)
            except (TypeError, ValueError):
                logging.warning(f"/Link/ChargeCurrent: cannot parse {value!r}")
                return True
            capped = max(0.0, min(target, dvcc_max_current_safety))
            if capped != target:
                logging.warning(f"DVCC ChargeCurrent {target} A capped to {capped} A (safety limit {dvcc_max_current_safety} A)")
            payload = "%.2f" % capped
            mqtt_client.publish(self._cmd_dc_current, payload)
            logging.info(f"DVCC → ESPHome: {self._cmd_dc_current} = {payload} A")

        elif path == "/Link/ChargeVoltage" and self._cmd_dc_voltage:
            try:
                target = float(value)
            except (TypeError, ValueError):
                logging.warning(f"/Link/ChargeVoltage: cannot parse {value!r}")
                return True
            if not (dvcc_min_voltage_safety <= target <= dvcc_max_voltage_safety):
                logging.warning(f"DVCC ChargeVoltage {target} V outside safety [{dvcc_min_voltage_safety}, {dvcc_max_voltage_safety}] — IGNORING")
                return True
            payload = "%.2f" % target
            mqtt_client.publish(self._cmd_dc_voltage, payload)
            logging.info(f"DVCC → ESPHome: {self._cmd_dc_voltage} = {payload} V")

        elif path == "/Link/NetworkMode":
            # Venus toggling DVCC mode; just accept the value
            pass

        return True


# ─────────────────────────── main ───────────────────────────
def main():
    global mqtt_client, topic_handlers

    _thread.daemon = True

    from dbus.mainloop.glib import DBusGMainLoop  # pyright: ignore[reportMissingImports]
    DBusGMainLoop(set_as_default=True)

    # Build topic_handlers from config.
    # All keys here must exist in config.ini [ESPHOME] section.
    esp = config["ESPHOME"] if "ESPHOME" in config else {}

    def _add(cfg_key, handler):
        topic = esp.get(cfg_key, "").strip()
        if topic:
            topic_handlers[topic] = handler

    _add("topic_dc_voltage",     _set_float("/Dc/0/Voltage"))
    _add("topic_dc_current",     _set_float("/Dc/0/Current"))
    _add("topic_dc_temperature", _set_float("/Dc/0/Temperature"))
    _add("topic_max_current",    _set_float("/Settings/ChargeCurrentLimit"))
    _add("topic_power_state",    _handle_power_state)
    _add("topic_lwt",            _handle_lwt)

    cmd_dc_current_topic = esp.get("topic_cmd_dc_current_limit", "").strip() or None
    cmd_dc_voltage_topic = esp.get("topic_cmd_voltage_limit",   "").strip() or None

    if not topic_handlers:
        logging.error("No ESPHome topics configured in [ESPHOME] section of config.ini. Exiting.")
        sys.exit(1)

    logging.info(f"Configured {len(topic_handlers)} subscribed topics, "
                 f"DVCC control {'ENABLED' if dvcc_enabled else 'disabled'}, "
                 f"cmd_dc_current={cmd_dc_current_topic}, cmd_dc_voltage={cmd_dc_voltage_topic}")

    # MQTT client
    mqtt_client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="MqttCharger_" + get_vrm_portal_id() + "_" + str(config["DEFAULT"]["device_instance"]),
    )
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    # TLS
    if config["MQTT"].get("tls_enabled", "0") == "1":
        logging.info("MQTT: TLS enabled")
        ca = config["MQTT"].get("tls_path_to_ca", "").strip()
        if ca:
            mqtt_client.tls_set(ca, tls_version=2)
        else:
            mqtt_client.tls_set(tls_version=2)
        if config["MQTT"].get("tls_insecure", "0") == "1":
            mqtt_client.tls_insecure_set(True)

    # Auth
    user = config["MQTT"].get("username", "").strip()
    pwd  = config["MQTT"].get("password", "").strip()
    if user and pwd:
        mqtt_client.username_pw_set(username=user, password=pwd)

    logging.info(f"MQTT: connecting to {config['MQTT']['broker_address']}:{config['MQTT']['broker_port']}")
    mqtt_client.connect(host=config["MQTT"]["broker_address"], port=int(config["MQTT"]["broker_port"]))
    mqtt_client.loop_start()

    # Wait for first DC current reading (the one we really need for accounting)
    i = 0
    while charger_dict["/Dc/0/Current"]["value"] is None:
        if i % 12 == 0 and i > 0:
            logging.warning("Still waiting for first /Dc/0/Current after %d s..." % (i * 5))
        else:
            logging.info("Waiting for first /Dc/0/Current MQTT message...")
        if timeout != 0 and timeout <= (i * 5):
            logging.error("No MQTT data within %d s — exiting." % timeout)
            sys.exit()
        sleep(5)
        i += 1

    paths_dbus = {"/UpdateIndex": {"value": 0, "textformat": _n}}
    paths_dbus.update(charger_dict)

    DbusMqttChargerService(
        servicename="com.victronenergy.charger.mqtt_charger_" + str(config["DEFAULT"]["device_instance"]),
        deviceinstance=int(config["DEFAULT"]["device_instance"]),
        customname=config["DEFAULT"].get("device_name", "MQTT Charger"),
        paths=paths_dbus,
        cmd_topic_dc_current=cmd_dc_current_topic,
        cmd_topic_dc_voltage=cmd_dc_voltage_topic,
    )

    logging.info("dbus registered, entering GLib MainLoop")
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
