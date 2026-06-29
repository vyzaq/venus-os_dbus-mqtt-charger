#!/usr/bin/env python
# dbus-mqtt-charger.py
#
# Bridges an ESPHome-controlled Huawei R4875G1 rectifier (or similar) to Venus
# OS as a com.victronenergy.charger service.
#
# Two-way bridge:
#   READ:  subscribes to multiple ESPHome MQTT state topics (one per sensor,
#          ESPHome's default behaviour) and republishes the data on dbus as
#          a com.victronenergy.charger service.
#   WRITE: _handlechangedvalue forwards /Link/ChargeCurrent and
#          /Link/ChargeVoltage written by Venus DVCC to ESPHome via MQTT.
#          Optional Plan B DvccForwarder (dvcc_control_enabled=1) adds
#          load-following logic; disable it if Venus DVCC is sufficient.
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
import dbus  # noqa: E402  (full dbus used by DvccForwarder)

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
_charger_offline = False    # True after timeout elapsed; service stays on dbus with zero values
_need_offline_push = False  # one-shot: force a dbus push when transitioning to offline


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
    "/Dc/0/Voltage": {"value": 0.0, "textformat": _v},
    "/Dc/0/Current": {"value": 0.0, "textformat": _a},
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


def _zero_charger_measurements():
    """Zero out measurement values when charger goes offline."""
    charger_dict["/Dc/0/Voltage"]["value"] = 0.0
    charger_dict["/Dc/0/Current"]["value"] = 0.0
    charger_dict["/Dc/0/Temperature"]["value"] = None


def _derive_state():
    """
    Choose /State value based on /Mode, current and DVCC NetworkMode.
    Simplified: we don't try to detect Absorption vs Float — the Huawei is
    operated as a power supply with externally-set voltage/current, so the
    most accurate Victron state is 11 (Power supply) or 252 (External control)
    when DVCC is writing setpoints.
    """
    if _charger_offline:
        return 0  # Off when no MQTT data
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


# ─────────────────────────── DVCC forwarder (Plan B) ───────────────────────────
class DvccForwarder:
    """
    Replicates a small piece of what DVCC does for solarchargers: takes the
    user-set and BMS-derived charge limits from the Venus SystemBus, picks
    the minimum, applies safety clamps, and forwards the result to ESPHome
    via MQTT.

    Why: Venus DVCC does NOT write /Link/ChargeCurrent on services of type
    com.victronenergy.charger (only on solarchargers and VE.Bus chargers).
    For a com.victronenergy.charger to follow DVCC limits we have to read
    the same source-of-truth values ourselves.

    Sources monitored (Venus v3.x):
      user_ccl: com.victronenergy.settings  /Settings/SystemSetup/MaxChargeCurrent
      user_cvl: com.victronenergy.settings  /Settings/SystemSetup/MaxChargeVoltage
      bms_ccl:  com.victronenergy.battery.aggregate  /Info/MaxChargeCurrent
      bms_cvl:  com.victronenergy.battery.aggregate  /Info/MaxChargeVoltage

    Result: active_ccl = min(user_ccl, bms_ccl), active_cvl = min(user_cvl, bms_cvl)
    Then clamped to safety limits and published to the ESPHome command topics.

    Updates are signal-driven (PropertiesChanged) with a periodic heartbeat
    re-evaluation as a fail-safe.
    """

    def __init__(self, mqtt_client, cmd_topic_ccl, cmd_topic_cvl,
                 paths, max_current, min_voltage, max_voltage,
                 mode="load_following",
                 hysteresis_ccl=1.0, hysteresis_cvl=0.05,
                 multi_dc_ema_alpha=0.25,
                 min_publish_interval_s=2.0,
                 heartbeat_s=30):
        self._mqtt = mqtt_client
        self._cmd_ccl = cmd_topic_ccl
        self._cmd_cvl = cmd_topic_cvl
        self._max_current = max_current
        self._min_voltage = min_voltage
        self._max_voltage = max_voltage
        self._mode = mode  # 'load_following' or 'simple'
        self._hysteresis_ccl = hysteresis_ccl
        self._hysteresis_cvl = hysteresis_cvl
        # Damping to break the Huawei↔MultiPlus feedback oscillation:
        # - EMA on multi_dc so we don't react to instant spikes
        # - min interval between publishes so we don't issue rapid-fire commands
        self._multi_dc_alpha = multi_dc_ema_alpha   # 0..1, higher = more reactive
        self._multi_dc_smoothed = None
        self._min_publish_interval = min_publish_interval_s
        self._last_publish_time = 0.0
        self._paths = paths  # list of (key, service, path) tuples
        self._values = {}    # {key: float or None}
        self._last_published_ccl = None
        self._last_published_cvl = None
        self._bus = dbus.SystemBus()
        self._setup_subscriptions()
        # Publish initial state once dbus mainloop is running.
        # We can call evaluate now; GLib hasn't entered yet but mqtt.publish
        # is thread-safe and will be queued.
        self._evaluate_and_publish()
        if heartbeat_s > 0:
            GLib.timeout_add_seconds(heartbeat_s, self._heartbeat)

    @staticmethod
    def _parse(raw):
        """Parse a dbus value to float or None.

        Venus represents 'not set' values as an empty dbus.Array (`[]`).
        """
        try:
            if isinstance(raw, dbus.Array):
                if len(raw) == 0:
                    return None
                raw = raw[0]
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _setup_subscriptions(self):
        # Group watched paths by service so we make one ItemsChanged
        # subscription per service (Victron services emit a single batched
        # `ItemsChanged` at root `/` rather than per-path PropertiesChanged).
        services_to_path_to_key = {}  # {service_name: {path: key}}
        for key, service, path in self._paths:
            services_to_path_to_key.setdefault(service, {})[path] = key
            # Initial GET for each watched path
            try:
                obj = self._bus.get_object(service, path)
                raw = obj.GetValue(dbus_interface='com.victronenergy.BusItem')
                self._values[key] = self._parse(raw)
                logging.info("DVCC init: %s = %s (from %s%s)",
                             key, self._values[key], service, path)
            except Exception as e:
                self._values[key] = None
                logging.warning("DVCC init: cannot read %s%s: %s", service, path, e)

        # Subscribe to ItemsChanged at root `/` of each watched service
        for service, path_to_key in services_to_path_to_key.items():
            try:
                self._bus.add_signal_receiver(
                    handler_function=(lambda items, s=service, p2k=path_to_key:
                                      self._on_items_changed(s, p2k, items)),
                    signal_name='ItemsChanged',
                    dbus_interface='com.victronenergy.BusItem',
                    bus_name=service,
                    path='/',
                )
                logging.info("DVCC subscribed to ItemsChanged on %s (watching: %s)",
                             service, list(path_to_key.keys()))
            except Exception as e:
                logging.warning("DVCC subscribe failed for %s: %s", service, e)

    def _on_items_changed(self, service, path_to_key, items):
        """
        Handler for /ItemsChanged. `items` is a dbus array decoded as a Python
        dict of the form:
            {'/Dc/0/Current': {'Value': -10.2, 'Text': '-10.2A'}, ...}
        We pick out only the paths we care about for this service.
        """
        if not isinstance(items, dict):
            return
        changed = False
        for path, fields in items.items():
            key = path_to_key.get(str(path))
            if key is None:
                continue
            if not isinstance(fields, dict) or 'Value' not in fields:
                continue
            new = self._parse(fields['Value'])
            old = self._values.get(key)
            if new == old:
                continue
            self._values[key] = new
            logging.info("DVCC change: %s: %s → %s", key, old, new)
            changed = True
        if changed:
            self._evaluate_and_publish()

    def _heartbeat(self):
        """Periodic re-evaluation + dbus refresh in case a signal was missed
        or a watched service has restarted."""
        for key, service, path in self._paths:
            try:
                obj = self._bus.get_object(service, path)
                raw = obj.GetValue(dbus_interface='com.victronenergy.BusItem')
                v = self._parse(raw)
                if v != self._values.get(key):
                    logging.info("DVCC heartbeat refresh: %s: %s → %s",
                                 key, self._values.get(key), v)
                    self._values[key] = v
            except Exception:
                # Service may be restarting — keep last known value, try again next tick.
                pass
        self._evaluate_and_publish()
        return True  # keep GLib timer alive

    def _evaluate_and_publish(self):
        """
        Compute Huawei setpoints and publish to ESPHome.

        ── CCL (current limit) ──
        target_net_charge = min(user_ccl, bms_ccl)
            — desired NET current into the battery (Victron convention)
            — user_ccl=0 means "don't charge", user_ccl=10 means "up to 10 A net into battery"

        mode = 'load_following' (default):
            Huawei_target = max(0, -multiplus_dc + target_net_charge)
            — multiplus_dc is signed: + when Multi charges battery, − when Multi inverts
            — Huawei covers whatever Multi is consuming from DC bus, plus the allowed
              net charging current. Result: net battery current = target_net_charge,
              regardless of inverter load.

        mode = 'simple':
            Huawei_target = target_net_charge
            — legacy/naïve mode. Use only when there's no MultiPlus or to debug.

        ── CVL (voltage limit) ──
        active_cvl = min(user_cvl, bms_cvl), clamped to safety window.
        """
        user_ccl = self._values.get('user_ccl')
        bms_ccl  = self._values.get('bms_ccl')
        user_cvl = self._values.get('user_cvl')
        bms_cvl  = self._values.get('bms_cvl')
        multi_dc_raw = self._values.get('multiplus_dc')

        # EMA-smooth multi_dc to damp out short-lived spikes (often caused by
        # our own setpoint changes inducing transient reactions in Multi).
        if multi_dc_raw is None:
            multi_dc = 0.0
        else:
            if self._multi_dc_smoothed is None:
                self._multi_dc_smoothed = multi_dc_raw
            else:
                self._multi_dc_smoothed = (
                    self._multi_dc_alpha * multi_dc_raw
                    + (1.0 - self._multi_dc_alpha) * self._multi_dc_smoothed
                )
            multi_dc = self._multi_dc_smoothed

        # Compute target net battery charge current
        ccl_inputs = [v for v in (user_ccl, bms_ccl) if v is not None]
        target_net_charge = min(ccl_inputs) if ccl_inputs else None

        # Apply mode
        if target_net_charge is None:
            active_ccl = None
        elif self._mode == 'load_following':
            active_ccl = -multi_dc + target_net_charge
            # Huawei can't sink current — if formula goes negative it means
            # Multi is charging hard enough by itself; we just sit at 0.
            active_ccl = max(0.0, active_ccl)
        else:  # 'simple'
            active_ccl = max(0.0, target_net_charge)

        # CVL — straight min, no Multi involvement
        cvl_inputs = [v for v in (user_cvl, bms_cvl) if v is not None]
        active_cvl = min(cvl_inputs) if cvl_inputs else None

        # Safety: clamp CCL to [0, max], drop CVL outside [min, max]
        if active_ccl is not None:
            clamped = min(active_ccl, self._max_current)
            if abs(clamped - active_ccl) > 1e-6:
                logging.warning("DVCC CCL %.2f A clamped to %.2f A (safety cap %.1f A)",
                                active_ccl, clamped, self._max_current)
                active_ccl = clamped

        if active_cvl is not None:
            if not (self._min_voltage <= active_cvl <= self._max_voltage):
                logging.warning("DVCC CVL %.2f V outside safety [%.2f..%.2f] — NOT publishing",
                                active_cvl, self._min_voltage, self._max_voltage)
                active_cvl = None

        # Publish to ESPHome with hysteresis AND rate-limit:
        #   - hysteresis: only publish if value moved by ≥ deadband
        #   - rate-limit: never publish more often than min_publish_interval_s,
        #     to give Multi+Huawei time to settle between commands and avoid
        #     control-loop oscillation
        now = time()
        time_since_last = now - self._last_publish_time
        rate_limited = time_since_last < self._min_publish_interval

        if self._cmd_ccl and active_ccl is not None:
            ccl_delta_ok = (self._last_published_ccl is None
                            or abs(active_ccl - self._last_published_ccl) >= self._hysteresis_ccl)
            if ccl_delta_ok and not rate_limited:
                payload = "%.2f" % active_ccl
                self._mqtt.publish(self._cmd_ccl, payload)
                logging.info("DVCC → ESPHome: %s = %s A "
                             "[mode=%s, user_ccl=%s, bms_ccl=%s, multi_dc_smoothed=%.2f, target_net=%s]",
                             self._cmd_ccl, payload, self._mode,
                             user_ccl, bms_ccl, multi_dc, target_net_charge)
                self._last_published_ccl = active_ccl
                self._last_publish_time = now

        if self._cmd_cvl and active_cvl is not None:
            cvl_delta_ok = (self._last_published_cvl is None
                            or abs(active_cvl - self._last_published_cvl) >= self._hysteresis_cvl)
            # voltage changes are rare and important — bypass rate-limit
            if cvl_delta_ok:
                payload = "%.2f" % active_cvl
                self._mqtt.publish(self._cmd_cvl, payload)
                logging.info("DVCC → ESPHome: %s = %s V [user_cvl=%s, bms_cvl=%s]",
                             self._cmd_cvl, payload, user_cvl, bms_cvl)
                self._last_published_cvl = active_cvl

        # Mirror into our own /Link/* paths so Venus UI shows the active values
        if active_ccl is not None:
            charger_dict['/Link/ChargeCurrent']['value'] = active_ccl
        if active_cvl is not None:
            charger_dict['/Link/ChargeVoltage']['value'] = active_cvl
        # NetworkMode = 0x0D (1 + 4 + 8): external control + external V + external I
        charger_dict['/Link/NetworkMode']['value'] = 13
        return True  # for GLib timer


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
        global last_changed, last_updated, _charger_offline, _need_offline_push
        now = int(time())

        timed_out = timeout != 0 and (now - last_changed) > timeout

        if timed_out and not _charger_offline:
            logging.warning("No MQTT data for %i s — charger offline, holding zero values on dbus.", timeout)
            _charger_offline = True
            _need_offline_push = True
            _zero_charger_measurements()
        elif not timed_out and _charger_offline:
            _charger_offline = False
            logging.info("MQTT data resumed after offline period.")

        if last_changed != last_updated or _need_offline_push:
            _need_offline_push = False
            # Derive /State from current snapshot
            charger_dict["/State"]["value"] = _derive_state()

            # Push everything from charger_dict to dbus
            for path, data in charger_dict.items():
                try:
                    self._dbusservice[path] = data["value"]
                except Exception:
                    et, eo, etb = sys.exc_info()
                    logging.error(f"dbus set {path}={data['value']} failed: {repr(eo)} in {etb.tb_frame.f_code.co_filename}:{etb.tb_lineno}")

            # /Connected: timeout or ESPHome LWT "offline" → 0
            self._dbusservice["/Connected"] = 0 if (_charger_offline or not _esphome_online) else 1

            v = charger_dict["/Dc/0/Voltage"]["value"] or 0
            i = charger_dict["/Dc/0/Current"]["value"] or 0
            logging.info("Charger: {:.2f} V × {:.2f} A = {:.0f} W (state={}, mode={}, offline={})".format(
                v, i, v * i,
                charger_dict["/State"]["value"],
                charger_dict["/Mode"]["value"],
                _charger_offline))

            last_updated = last_changed

        # UpdateIndex tick
        idx = (self._dbusservice["/UpdateIndex"] + 1) & 0xFF
        self._dbusservice["/UpdateIndex"] = idx
        return True

    def _handlechangedvalue(self, path, value):
        """
        DVCC forwarding is now done by DvccForwarder (Plan B), which reads
        directly from com.victronenergy.settings and com.victronenergy.battery.aggregate.
        Venus does NOT write to /Link/* on com.victronenergy.charger services,
        so this handler only logs writes for debugging.
        """
        logging.debug("dbus set %s = %s", path, value)
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
    mqtt_client.loop_start()
    try:
        mqtt_client.connect(host=config["MQTT"]["broker_address"], port=int(config["MQTT"]["broker_port"]))
    except Exception as err:
        logging.warning(f"MQTT: initial connect failed ({err}), will retry in background via on_disconnect")

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

    # ── Plan B: DVCC forwarder. Reads CCL/CVL from settings + aggregate BMS
    # + MultiPlus DC current, computes load-following setpoint, publishes to ESPHome.
    if dvcc_enabled and (cmd_dc_current_topic or cmd_dc_voltage_topic):
        dvcc_section = config["DVCC"] if "DVCC" in config else {}
        # Each source: (key, service_name, dbus_path)
        dvcc_paths = [
            (
                "user_ccl",
                dvcc_section.get("user_ccl_service", "com.victronenergy.settings"),
                dvcc_section.get("user_ccl_path", "/Settings/SystemSetup/MaxChargeCurrent"),
            ),
            (
                "user_cvl",
                dvcc_section.get("user_cvl_service", "com.victronenergy.settings"),
                dvcc_section.get("user_cvl_path", "/Settings/SystemSetup/MaxChargeVoltage"),
            ),
            (
                "bms_ccl",
                dvcc_section.get("bms_service", "com.victronenergy.battery.aggregate"),
                dvcc_section.get("bms_ccl_path", "/Info/MaxChargeCurrent"),
            ),
            (
                "bms_cvl",
                dvcc_section.get("bms_service", "com.victronenergy.battery.aggregate"),
                dvcc_section.get("bms_cvl_path", "/Info/MaxChargeVoltage"),
            ),
            (
                "multiplus_dc",
                dvcc_section.get("multiplus_service", "com.victronenergy.system"),
                dvcc_section.get("multiplus_dc_path", "/Dc/Vebus/Current"),
            ),
        ]
        heartbeat_s = int(dvcc_section.get("heartbeat_seconds", "30"))
        mode = dvcc_section.get("mode", "load_following").strip()
        if mode not in ("load_following", "simple"):
            logging.warning("Unknown DVCC mode '%s', falling back to 'load_following'", mode)
            mode = "load_following"
        hysteresis_ccl = float(dvcc_section.get("hysteresis_ccl", "1.0"))
        hysteresis_cvl = float(dvcc_section.get("hysteresis_cvl", "0.05"))
        multi_dc_alpha = float(dvcc_section.get("multi_dc_ema_alpha", "0.25"))
        min_publish_interval = float(dvcc_section.get("min_publish_interval_seconds", "2.0"))
        DvccForwarder(
            mqtt_client=mqtt_client,
            cmd_topic_ccl=cmd_dc_current_topic,
            cmd_topic_cvl=cmd_dc_voltage_topic,
            paths=dvcc_paths,
            max_current=dvcc_max_current_safety,
            min_voltage=dvcc_min_voltage_safety,
            max_voltage=dvcc_max_voltage_safety,
            mode=mode,
            hysteresis_ccl=hysteresis_ccl,
            hysteresis_cvl=hysteresis_cvl,
            multi_dc_ema_alpha=multi_dc_alpha,
            min_publish_interval_s=min_publish_interval,
            heartbeat_s=heartbeat_s,
        )
        logging.info("DVCC forwarder started (mode=%s, hysteresis_ccl=%.2f A, "
                     "hysteresis_cvl=%.2f V, ema_alpha=%.2f, min_publish_interval=%.1fs, heartbeat=%ds)",
                     mode, hysteresis_ccl, hysteresis_cvl,
                     multi_dc_alpha, min_publish_interval, heartbeat_s)
    else:
        logging.info("DVCC forwarder NOT started (dvcc_control_enabled=%s, cmd topics: ccl=%s cvl=%s)",
                     dvcc_enabled, cmd_dc_current_topic, cmd_dc_voltage_topic)

    logging.info("dbus registered, entering GLib MainLoop")
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
