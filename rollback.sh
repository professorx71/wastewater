#!/usr/bin/env bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Use sudo bash rollback.sh FULL_BACKUP_DIRECTORY'; exit 1; }
BACKUP="${1:-}"
[ -n "$BACKUP" ] && [ -f "$BACKUP/web/web.py" ] || { echo 'Specify valid pre-deploy-* backup directory'; exit 1; }
TIMER_WAS_ACTIVE=0
if systemctl is-active --quiet pbf-wastewater-scheduler.timer; then TIMER_WAS_ACTIVE=1; fi
systemctl stop pbf-wastewater-scheduler.timer || true
cp -a "$BACKUP/web/." /opt/pbf-wastewater/web/
systemctl restart pbf-wastewater-web
systemctl is-active --quiet pbf-wastewater-web
if [ "$TIMER_WAS_ACTIVE" = 1 ]; then systemctl start pbf-wastewater-scheduler.timer; fi
echo "Previous web code restored from $BACKUP"
echo 'History database, metadata, email configuration and Talk2M collector were not overwritten.'
