#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
SERVICE_NAME=$(basename $SCRIPT_DIR)

echo
echo "Installing $SERVICE_NAME..."

# set permissions for script files
echo "Setting permissions..."
chmod 755 $SCRIPT_DIR/$SERVICE_NAME.py
chmod 755 $SCRIPT_DIR/install.sh
chmod 755 $SCRIPT_DIR/restart.sh
chmod 755 $SCRIPT_DIR/uninstall.sh
chmod 755 $SCRIPT_DIR/get-logs.sh
chmod 755 $SCRIPT_DIR/service/run
chmod 755 $SCRIPT_DIR/service/log/run

# check config.ini
if [ ! -f "$SCRIPT_DIR/config.ini" ]; then
    echo
    echo "ERROR: config.ini not found."
    echo "Copy config.sample.ini to config.ini and fill in your settings before installing."
    echo
    exit 1
fi

# check dependencies (paho-mqtt is bundled in ext/, this is a fallback)
python -c "import paho.mqtt.client" 2>/dev/null
if [ $? -gt 0 ]; then
    echo "Installing paho.mqtt.client..."
    python -m pip install paho-mqtt
    if [ $? -gt 0 ]; then
        opkg update && opkg install python3-pip
        python -m pip install paho-mqtt
    fi
fi

# create log directory for multilog (must exist before supervise starts log/run)
mkdir -p /var/log/$SERVICE_NAME

# stop and kill all old instances before (re)installing the symlink
if [ -L /service/$SERVICE_NAME ]; then
    echo "Stopping existing service..."
    svc -d /service/$SERVICE_NAME 2>/dev/null || true
fi
pkill -f "supervise $SERVICE_NAME"              2>/dev/null || true
pkill -f "multilog .* /var/log/$SERVICE_NAME"   2>/dev/null || true
pkill -f "python .*/etc/$SERVICE_NAME/$SERVICE_NAME.py" 2>/dev/null || true
sleep 1

# (re)create sym-link to run script in daemon
if [ -L /service/$SERVICE_NAME ]; then
    rm /service/$SERVICE_NAME
fi
echo "Creating service symlink..."
ln -s $SCRIPT_DIR/service /service/$SERVICE_NAME

# add install-script to rc.local to survive firmware updates
filename=/data/rc.local
if [ ! -f $filename ]; then
    touch $filename
    chmod 755 $filename
    echo "#!/bin/bash" >> $filename
    echo >> $filename
fi
grep -qxF "bash $SCRIPT_DIR/install.sh" $filename || echo "bash $SCRIPT_DIR/install.sh" >> $filename

# ── dbus-aggregate-batteries: set MPPT_KEYWORD ────────────────────────────────
# This driver registers as com.victronenergy.charger (not solarcharger).
# The aggregate battery must search for the correct service type.
AGG_CONFIG="/data/apps/dbus-aggregate-batteries/config.ini"
if [ -f "$AGG_CONFIG" ]; then
    if grep -q "MPPT_KEYWORD = com.victronenergy.solarcharger" "$AGG_CONFIG"; then
        echo "Setting MPPT_KEYWORD = com.victronenergy.charger in $AGG_CONFIG..."
        sed -i "s|MPPT_KEYWORD = com.victronenergy.solarcharger|MPPT_KEYWORD = com.victronenergy.charger|" "$AGG_CONFIG"
        echo "  OK"
        if [ -L /service/dbus-aggregate-batteries ]; then
            echo "Restarting dbus-aggregate-batteries..."
            svc -t /service/dbus-aggregate-batteries
        fi
    elif grep -q "MPPT_KEYWORD = com.victronenergy.charger" "$AGG_CONFIG"; then
        echo "MPPT_KEYWORD already set correctly."
    else
        echo "  NOTE: set MPPT_KEYWORD = com.victronenergy.charger in $AGG_CONFIG manually."
    fi
else
    echo "dbus-aggregate-batteries not found — skipping MPPT_KEYWORD config."
fi
# ──────────────────────────────────────────────────────────────────────────────

echo
echo "Installation complete."
echo
