# dbus-mqtt-charger - Emulates a physical AC→DC battery charger from MQTT data

<small>Forked from [mr-manuel/venus-os_dbus-mqtt-solar-charger](https://github.com/mr-manuel/venus-os_dbus-mqtt-solar-charger) and converted to publish as `com.victronenergy.charger` (regular AC→DC charger, e.g. Phoenix Charger / Skylla) instead of `com.victronenergy.solarcharger`.</small>

## Index

1. [Disclaimer](#disclaimer)
1. [Purpose](#purpose)
1. [Why not solar-charger](#why-not-solar-charger)
1. [Config](#config)
1. [JSON structure](#json-structure)
1. [Install / Update](#install--update)
1. [Uninstall](#uninstall)
1. [Restart](#restart)
1. [Debugging](#debugging)
1. [Compatibility](#compatibility)


## Disclaimer

I am not responsible if you damage something using this script.


## Purpose

The script emulates a regular AC→DC battery charger in Venus OS (Phoenix Charger / Skylla family). It subscribes to an MQTT topic and publishes the data on the dbus as service:

```
com.victronenergy.charger.mqtt_charger_<device_instance>
```

Intended use case: an external AC→DC charger (such as a Huawei R4875G1 rectifier driven by ESPHome) connected directly to the battery bus, when the MultiPlus is out of its AC input range (e.g. grid voltage above 260 V at night) but the rectifier can still operate (Huawei range is 85–300 V).

When the rectifier is registered on Venus as a `charger`, the DC overview balances correctly: battery current = MultiPlus DC + charger DC + DC system. SOC is still measured by the BMS shunt (in this case JKBMS via dbus-serialbattery), so this driver is purely about giving Venus the right picture of where the current is coming from.


## Why not solar-charger

`com.victronenergy.solarcharger` works for the DC balance but mis-attributes energy to "Solar" in VRM and exposes PV/MPPT fields that have no meaning for an AC→DC rectifier. `com.victronenergy.charger` is the semantically correct service type and keeps the VRM statistics clean.


## Config

Copy or rename `config.sample.ini` to `config.ini` inside the `dbus-mqtt-charger` folder and adjust:

- `[MQTT] broker_address` — IP or FQDN of your MQTT broker (on Venus the local Mosquitto is `127.0.0.1`)
- `[MQTT] topic` — topic where your ESPHome / external publisher posts the charger JSON
- `[DEFAULT] device_name` — display name in the Venus UI (e.g. `Huawei R4875G1`)
- `[DEFAULT] device_instance` — VRM instance number (must be unique)


## JSON structure

### Minimum required

```json
{
    "Dc": {
        "0": {
            "Voltage": 53.5,
            "Current": 25.3
        }
    }
}
```

`/State` defaults to Bulk (3) when current > 0.1 A and Off (0) otherwise. `/Mode` defaults to On (1). `/NrOfOutputs` defaults to 1 (or inferred from how many `Dc/N` blocks are present).

### Full

See the [Battery chargers section](https://github.com/victronenergy/venus/wiki/dbus#battery-chargers) of the Victron dbus wiki for value semantics.

```json
{
    "Dc": {
        "0": {
            "Voltage": 53.5,                   // Float - V on output 0
            "Current": 25.3,                   // Float - A on output 0
            "Temperature": 35.0                // Float - °C on output 0 (optional)
        },
        "1": {                                 // optional - second output
            "Voltage": 53.5,
            "Current": 0.0
        },
        "2": {                                 // optional - third output
            "Voltage": 53.5,
            "Current": 0.0
        }
    },
    "NrOfOutputs": 1,                          // Int - 1..3
    "Mode": 1,                                 // Int - 1 = On, 4 = Off
    "State": 3,                                // Int - 0 = Off, 2 = Fault, 3 = Bulk,
                                               //       4 = Absorption, 5 = Float,
                                               //       6 = Storage, 7 = Equalize,
                                               //       8 = Passthru, 9 = Inverting,
                                               //       11 = Power supply,
                                               //       245 = Wake-up,
                                               //       252 = External control
    "ErrorCode": 0,                            // Int - Victron error code
    "Relay": {
        "0": { "State": 0 }                    // Int - 0 = Open, 1 = Closed
    },
    "Settings": {
        "ChargeCurrentLimit": 50.0             // Float - A
    },
    "Link": {                                  // optional - DVCC / external control
        "NetworkMode": "0x1",
        "ChargeCurrent": 0.0,
        "ChargeVoltage": 0.0,
        "TemperatureSense": 25.0,
        "TemperatureSenseActive": 0,
        "VoltageSense": 53.5,
        "VoltageSenseActive": 0
    },
    "History": {
        "EnergyOut": 12.34                     // Float - lifetime kWh out
    }
}
```


## Install / Update

1. Login to your Venus OS device via SSH. See [Venus OS: Root Access](https://www.victronenergy.com/live/ccgx:root_access#root_access).

2. Either run the updated `download.sh` from your fork (if you publish it as `your-user/venus-os_dbus-mqtt-charger`), or manually:

    ```bash
    # copy the dbus-mqtt-charger folder to /data/etc/dbus-mqtt-charger
    scp -r dbus-mqtt-charger root@<venus-ip>:/data/etc/
    ```

3. Edit the config file:

    ```bash
    cp /data/etc/dbus-mqtt-charger/config.sample.ini /data/etc/dbus-mqtt-charger/config.ini
    nano /data/etc/dbus-mqtt-charger/config.ini
    ```

4. Install the driver as a service:

    ```bash
    bash /data/etc/dbus-mqtt-charger/install.sh
    ```

    daemon-tools picks it up within a few seconds.


## Uninstall

```bash
bash /data/etc/dbus-mqtt-charger/uninstall.sh
```


## Restart

```bash
bash /data/etc/dbus-mqtt-charger/restart.sh
```


## Debugging

Tail the live log:

```bash
tail -n 100 -F /data/log/dbus-mqtt-charger/current | tai64nlocal
```

Check service status:

```bash
svstat /service/dbus-mqtt-charger
```

If `seconds` stays under 5 the script is crash-looping. Set `logging = DEBUG` in `config.ini` or temporarily change `level=logging.WARNING` in the .py file.

If you see `dbus.exceptions.NameExistsException: Bus name already exists: com.victronenergy.charger.mqtt_charger_*` it means another instance with the same `device_instance` is already running — change the instance number or stop the duplicate.


## Compatibility

Tested approach mirrors the upstream solar-charger driver, which supports the latest three stable Venus OS releases.

## Test Environment
Convert to com.victronenergy.charger with DVCC load-following

- Replace solarcharger service type with com.victronenergy.charger
- Subscribe to ESPHome state topics (multi-topic, no JSON aggregation)
- Plan B: DVCC forwarding via direct dbus subscription (Venus doesn't write
  /Link/* on com.victronenergy.charger services)
- Load-following formula: max(0, -multi_dc + min(user_ccl, bms_ccl))
- ItemsChanged signal subscription (PropertiesChanged on per-path doesn't fire)
- EMA smoothing (alpha=0.1) + hysteresis (2.0A) + rate-limiting (3s) for
  feedback oscillation prevention with cyclic loads (heater+AC, fridge etc.)
- Tested on Venus v3.72, MultiPlus-II 48/6k5, 2x JKBMS via dbus-serialbattery,
  Dr-Gigavolt dbus-aggregate-batteries, Huawei R4875G1 via ESPHome
