Wastewater Dashboard — Rev 1.2.59
Design and program by Daniel Salazar.

NEVER run Linux scripts in Windows PowerShell before ssh-ing into the Ubuntu VPS.
No deployment occurs simply by downloading this archive.

RELEASE NOTES
- Live API-backed top telemetry and embedded EQ tank overlays.
- Removed embedded 6-hour trend and alarm-history pictures, and redundant image of four cards.
- Retained actual trends on Trends page and event list on Alarms page.
- Configurable pH alarm confirmation and recovery (default 2 DISTINCT fresh samples each).
- Incidents are uniquely numbered; repeat independent incidents can each generate an email.
- Optional email upon recovery; production email credentials and reports preserved.
- Only mapped pH, EQ level, discharge flow, total flow are live. Motor frequency, pump operation,
  chemical dosing, sludge pickup, and tank-water fill remain schematics/not connected.

INSTALL TO STAGING FIRST
1) In Windows PowerShell:
   scp "$env:USERPROFILE\Downloads\pbf-wastewater-rev1.2.59.tar.gz" ubuntu@51.161.52.12:/home/ubuntu/
2) SSH:
   ssh ubuntu@51.161.52.12
3) On Ubuntu:
   mkdir -p ~/ww-upgrade
   tar xzf ~/pbf-wastewater-rev1.2.59.tar.gz -C ~/ww-upgrade
   cd ~/ww-upgrade/pbf-wastewater-rev1.2.59
   sudo bash stage.sh
4) Follow staging command printed by stage.sh; Gunicorn port 8003 must be launched in an SSH terminal.
   Staging uses a NEW metadata SQLite file with all email switches OFF by default.
5) Test http://127.0.0.1:8003/api/summary using curl -u wastewater and your actual password.
   For preview, tunnel Windows: ssh -L 8003:127.0.0.1:8003 ubuntu@51.161.52.12
   Open http://localhost:8003, confirm process and navigation.
6) Once staging checks pass, explicit authorization to switch production:
   sudo bash deploy.sh --approved
   Deployment backs up existing web app, historical database and metadata. Stops/restarts ONLY
   web service and reporting scheduler; does not stop/modify Talk2M collector.
7) Validate production: curl -u wastewater http://127.0.0.1:8001/api/summary
   sudo systemctl is-active pbf-wastewater-web pbf-wastewater-collector.timer pbf-wastewater-scheduler.timer
   sudo journalctl -u pbf-wastewater-scheduler.service -n 20 --no-pager
   Check https://51.161.52.12 for production UI and confirm alarm / daily report emails.

ROLLBACK
The deploy script prints a backup path, e.g.
/var/backups/pbf-wastewater/pre-deploy-1.2.59-YYYYMMDD-HHMMSS
To restore old web code (leaves history/metadata intact):
   sudo bash rollback.sh /var/backups/pbf-wastewater/pre-deploy-1.2.59-YYYYMMDD-HHMMSS

SECURITY AND LIMITATIONS
- Never paste SMTP passwords, HTTP Basic passwords or Talk2M API secrets in chat.
- Uses same HTTPS reverse proxy and Basic Auth as existing dashboard.
- Recovery email requires that initial incident email was recorded as sent.
- Daily report at 08:00 Pacific sends previous-day statistics; first-of-month report previous month.
- Confirmed alarms may take at least two distinct fresh readings, generally about two collection cycles.
- Existing alarm incident adopted at first run; if an old 1.2.0 incident is active, new alerts wait for recovery.
- Alerts are NOT a safety-related control or substitute for an independent plant monitoring/alarm system.

DEPLOYMENT CHECK FIX (2026-11-17)
- After restarting Gunicorn, deploy.sh waits for an authenticated-protected HTTP endpoint
  to respond (unauthenticated 401 expected), retrying for up to approximately 40 seconds.
- The prior Rev 1.2.1 deploy.sh immediately requested port 8001 after restart,
  which could trigger a premature rollback while Gunicorn was still opening the port.
- Prints systemd status and recent Gunicorn logs before rollback if the service never responds.
