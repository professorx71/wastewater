#!/usr/bin/env bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo: sudo bash stage.sh'; exit 1; }
cd "$(dirname "$0")"
python3 -m py_compile web.py collector/collector.py collector/live.py
python3 -m unittest test_revision -q
for f in /opt/pbf-wastewater/web/web.py /var/lib/pbf-wastewater/history.sqlite3 /etc/pbf-wastewater/web-auth.json; do
  test -f "$f" || { echo "Required file missing: $f"; exit 1; }
done
STAGE=/opt/pbf-wastewater/rev1.2.59-stage
install -d -m 0755 "$STAGE/static"
install -m 0644 web.py REVISION.txt "$STAGE/"
install -m 0644 static/* "$STAGE/static/"
install -d -o pbf-wastewater -g pbf-wastewater -m 0750 /var/lib/pbf-wastewater
STAGE_DB=/var/lib/pbf-wastewater/dashboard-v1.2.59-stage.sqlite3
sudo -u pbf-wastewater env PBF_APP_DB="$STAGE_DB" python3 "$STAGE/web.py" --init
cat <<'EOF'

Staging installed. Production remains untouched.
To launch staging in an SSH terminal:
 sudo -u pbf-wastewater env PBF_APP_DB=/var/lib/pbf-wastewater/dashboard-v1.2.59-stage.sqlite3 bash -c 'cd /opt/pbf-wastewater/rev1.2.59-stage && exec gunicorn --bind 127.0.0.1:8003 --workers 1 web:application'
In another SSH terminal:
 curl -i -u wastewater http://127.0.0.1:8003/api/summary
To preview in your Windows browser, establish tunnel:
 ssh -L 8003:127.0.0.1:8003 ubuntu@51.161.52.12
 Then browse http://localhost:8003

No staging SMTP reports are enabled by default.
EOF
