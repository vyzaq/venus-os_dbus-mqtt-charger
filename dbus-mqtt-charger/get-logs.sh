#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
SERVICE_NAME=$(basename $SCRIPT_DIR)

# fetch older (rotated) logs
cat /var/log/$SERVICE_NAME/@* 2>/dev/null | tai64nlocal

# follow live log
tail -F -n +1 /var/log/$SERVICE_NAME/current | tai64nlocal
