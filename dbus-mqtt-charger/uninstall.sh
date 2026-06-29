#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
SERVICE_NAME=$(basename $SCRIPT_DIR)

if [ -z "$SERVICE_NAME" ]; then
    echo "Error: SERVICE_NAME is not set."
    exit 1
fi

echo
echo "Uninstalling $SERVICE_NAME..."

# Remove from rc.local
echo "Removing from rc.local..."
sed -i "/$SERVICE_NAME/d" /data/rc.local 2>/dev/null || true

# Stop and remove service
echo "Stopping service..."
svc -d /service/$SERVICE_NAME 2>/dev/null || true
sleep 1
rm -f /service/$SERVICE_NAME

pkill -f "supervise .*$SERVICE_NAME"  2>/dev/null || true
pkill -f "multilog .*$SERVICE_NAME"   2>/dev/null || true
pkill -f "python .*$SERVICE_NAME"     2>/dev/null || true

# ── dbus-aggregate-batteries: revert MPPT_KEYWORD ─────────────────────────────
AGG_CONFIG="/data/apps/dbus-aggregate-batteries/config.ini"
if [ -f "$AGG_CONFIG" ] && grep -q "MPPT_KEYWORD = com.victronenergy.charger" "$AGG_CONFIG"; then
    echo "Reverting MPPT_KEYWORD in $AGG_CONFIG..."
    sed -i "s|MPPT_KEYWORD = com.victronenergy.charger|MPPT_KEYWORD = com.victronenergy.solarcharger|" "$AGG_CONFIG"
    echo "  OK"
    if [ -L /service/dbus-aggregate-batteries ]; then
        echo "Restarting dbus-aggregate-batteries..."
        svc -t /service/dbus-aggregate-batteries
    fi
fi
# ──────────────────────────────────────────────────────────────────────────────

echo "done."
echo

echo "Do you also want to delete all driver files including the config? [y/N]"
read -r DELETE_FILES
if [[ "$DELETE_FILES" == "y" || "$DELETE_FILES" == "Y" ]]; then
    echo "Deleting all driver files..."
    rm -rf "$SCRIPT_DIR"
    echo "done."
else
    echo "Driver files not deleted."
fi

echo
echo "*** Please reboot your device to complete the uninstallation. ***"
echo
