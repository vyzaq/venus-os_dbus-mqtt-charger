#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
SERVICE_NAME=$(basename $SCRIPT_DIR)

echo
echo "Restarting $SERVICE_NAME..."

if [ ! -L /service/$SERVICE_NAME ]; then
    echo "Service not installed. Run install.sh first."
    echo
    exit 1
fi

svc -d /service/$SERVICE_NAME 2>/dev/null || true
pkill -f "python .*/etc/$SERVICE_NAME/$SERVICE_NAME.py" 2>/dev/null || true
sleep 1
svc -u /service/$SERVICE_NAME
echo "done."
echo
