#!/usr/bin/env bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Use sudo bash deploy.sh --approved'; exit 1; }
[ "${1:-}" = '--approved' ] || { echo 'Requires explicit --approved after staging review'; exit 1; }
cd "$(dirname "$0")"
python3 -m py_compile web.py
bash -n stage.sh rollback.sh
python3 -m unittest test_revision -q
systemctl is-active --quiet pbf-wastewater-web || { echo 'Web service not active, refusing deployment'; exit 1; }
for f in /opt/pbf-wastewater/web/web.py /var/lib/pbf-wastewater/history.sqlite3 /etc/pbf-wastewater/web-auth.json; do
  test -f "$f" || { echo "Missing required file: $f"; exit 1; }
done
BACKUP="/var/backups/pbf-wastewater/pre-deploy-1.2.4-$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 "$BACKUP"
cp -a /opt/pbf-wastewater/web "$BACKUP/web"
cp -p /etc/pbf-wastewater/web-auth.json "$BACKUP/web-auth.json"
# SQLite online backups are consistent even when the collector is writing.
python3 - "$BACKUP" <<'PY'
import sqlite3,sys
from pathlib import Path
backup=Path(sys.argv[1]);folder=Path('/var/lib/pbf-wastewater')
for src,name in [(folder/'history.sqlite3','history-consistent.sqlite3'),(folder/'dashboard-v1.2.sqlite3','metadata-consistent.sqlite3')]:
    if not src.exists():continue
    with sqlite3.connect(f'file:{src}?mode=ro',uri=True) as inp:
        with sqlite3.connect(backup/name) as out:inp.backup(out)
    with sqlite3.connect(backup/name) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok',f'Invalid backup {name}'
    (backup/name).chmod(0o600)
print('Consistent SQLite backups verified')
PY
TIMER_WAS_ACTIVE=0
if systemctl is-active --quiet pbf-wastewater-scheduler.timer; then TIMER_WAS_ACTIVE=1; fi
systemctl stop pbf-wastewater-scheduler.timer || true
rollback_on_failure(){
  echo 'Production deployment failed — restoring previous dashboard code.' >&2
  cp -a "$BACKUP/web/." /opt/pbf-wastewater/web/
  systemctl restart pbf-wastewater-web || true
  if [ "$TIMER_WAS_ACTIVE" = 1 ]; then systemctl start pbf-wastewater-scheduler.timer || true; fi
  echo "Rollback backup: $BACKUP" >&2
  exit 1
}
# Existing collector, historical records, metadata, SMTP, and passwords are NOT changed.
install -m 0644 web.py /opt/pbf-wastewater/web/web.py || rollback_on_failure
install -m 0644 static/* /opt/pbf-wastewater/web/static/ || rollback_on_failure
install -m 0644 REVISION.txt /opt/pbf-wastewater/web/REVISION.txt || rollback_on_failure
sudo -u pbf-wastewater python3 /opt/pbf-wastewater/web/web.py --init || rollback_on_failure
systemctl restart pbf-wastewater-web || rollback_on_failure
systemctl is-active --quiet pbf-wastewater-web || rollback_on_failure
# Check code and live data source through the exact installed Python path.
sudo -u pbf-wastewater bash -c 'cd /opt/pbf-wastewater/web && python3 -c "from web import summary; d=summary(); assert d[\"revision\"]==\"1.2.4\" and len(d[\"tags\"])==4; print(\"Rev\",d[\"revision\"],\"four tags OK\")"' || rollback_on_failure
# Wait for Gunicorn to accept requests; systemd active does not mean HTTP is ready yet.
# A 401 is the EXPECTED reply for an unauthenticated /api/summary request.
READY=0
LAST_CODE='not checked'
for attempt in $(seq 1 40); do
  LAST_CODE="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 3 \
    http://127.0.0.1:8001/api/summary || true)"
  if [ "$LAST_CODE" = '401' ]; then
    READY=1
    echo "Production HTTP authentication check passed (401 as expected), attempt $attempt"
    break
  fi
  sleep 1
done
if [ "$READY" -ne 1 ]; then
  echo "Production HTTP did not become ready after 40 checks; last HTTP status: $LAST_CODE" >&2
  systemctl status pbf-wastewater-web --no-pager -l >&2 || true
  journalctl -u pbf-wastewater-web -n 45 --no-pager >&2 || true
  rollback_on_failure
fi
if [ "$TIMER_WAS_ACTIVE" = 1 ]; then systemctl start pbf-wastewater-scheduler.timer || rollback_on_failure; fi
printf '\nRevision 1.2.4 deployed. Backup: %s\n' "$BACKUP"
echo 'Existing production settings and email recipients preserved. Alarm confirmation defaults: 2 readings.'
echo 'Verify logged-in production at https://51.161.52.12, and check timer/journal/email delivery.'
