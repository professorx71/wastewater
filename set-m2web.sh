#!/usr/bin/env bash
# Run ON THE SERVER (after ssh), from the release folder:  sudo bash set-m2web.sh
# Saves the Talk2M M2Web login for live readings in /etc/pbf-wastewater/m2web.json (root only, 0600),
# tests it once, then starts the live readings service. Passwords are typed hidden and never shown.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo: sudo bash set-m2web.sh'; exit 1; }
cd "$(dirname "$0")"
FILE=/etc/pbf-wastewater/m2web.json
NEW=y
if [ -f "$FILE" ]; then
  read -r -p 'A login is already saved. Type y to enter new details, or press Enter to test the saved one: ' NEW
fi
if [ "${NEW:-n}" = y ] || [ "${NEW:-n}" = Y ]; then
echo 'Talk2M M2Web login for live readings (every field is required).'
read -r  -p 'Talk2M account name: ' ACCOUNT
read -r  -p 'Talk2M user name: ' T2MUSER
read -r -s -p 'Talk2M password (hidden): ' T2MPASS; echo
read -r -s -p 'Talk2M developer ID (hidden): ' DEVID; echo
read -r  -p 'Ewon name in Talk2M (as shown in eCatcher): ' EWON
read -r  -p 'Ewon login user (Ewon web page user): ' DEVUSER
read -r -s -p 'Ewon login password (hidden): ' DEVPASS; echo
read -r  -p 'Seconds between live reads, 2-300 [5]: ' INTERVAL
INTERVAL=${INTERVAL:-5}
umask 077
install -d -m 0750 /etc/pbf-wastewater
ACCOUNT="$ACCOUNT" T2MUSER="$T2MUSER" T2MPASS="$T2MPASS" DEVID="$DEVID" EWON="$EWON" DEVUSER="$DEVUSER" DEVPASS="$DEVPASS" INTERVAL="$INTERVAL" \
python3 - "$FILE" <<'PY'
import json,os,sys
e=os.environ
cfg={'account':e['ACCOUNT'],'username':e['T2MUSER'],'password':e['T2MPASS'],'developer_id':e['DEVID'],
     'ewon_name':e['EWON'],'device_username':e['DEVUSER'],'device_password':e['DEVPASS'],'interval':float(e['INTERVAL'])}
missing=[k for k,v in cfg.items() if v in ('',None)]
if missing:sys.exit('Missing: '+', '.join(missing)+'. Nothing saved.')
with open(sys.argv[1],'w') as f:json.dump(cfg,f)
os.chmod(sys.argv[1],0o600)
print('Saved',sys.argv[1],'(root only).')
PY
fi
echo; echo 'Testing one live read...'
if python3 collector/live.py --test; then
  install -m 0644 collector/live.py /opt/pbf-wastewater/live.py
  install -m 0644 pbf-wastewater-live.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now pbf-wastewater-live.service
  systemctl restart pbf-wastewater-live.service
  sleep 3; systemctl --no-pager --lines=5 status pbf-wastewater-live.service || true
  echo; echo 'Live readings are running. The cards show "live from the Ewon" when you hover over them.'
else
  echo; echo 'The test read failed: the service was NOT started. Check the names and passwords and run this again.'
  exit 1
fi
