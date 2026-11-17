# Deploying an update to the OVHcloud VPS (51.161.52.12)

This updates the **existing** production dashboard at https://51.161.52.12. It replaces only the web code (`web.py`, `static/`, `REVISION.txt`). It does **not** change the Talk2M collector, history database, settings/email metadata, SMTP config or passwords.

The workflow is: **package on Windows → upload → stage and preview → deploy → verify**. If anything looks wrong, roll back (step 7).

> Run the Windows steps in **PowerShell**. Run the `.sh` scripts **only on the Ubuntu VPS**, after you SSH in.

---

## 0. Before you start

- SSH access works: `ssh ubuntu@51.161.52.12`
- You know the production dashboard login (user `wastewater`). You'll need it to preview staging.
- You have a quiet moment: the deploy restarts the web service for a few seconds and pauses the alarm/report timer while it runs.

## 1. Commit the changes (Windows)

The release package is built from the committed code, so commit first:

```powershell
cd C:\developer\wastewater
git status
git add -A
git commit -m "Dashboard: redesigned top reading cards"
```

Check `git status` first: there must be no `.sqlite3`, `*auth.json`, `email.json` or `.local/` files. They're git-ignored, so they shouldn't appear.

> Running `py -m unittest test_revision` on Windows shows errors (`No module named 'tzdata'`, `WinError 32` file locks). These are Windows-only. The tests run on Linux in step 4 and in GitHub Actions, so those are the results that count.

## 2. Build the release package (Windows)

```powershell
git -c core.autocrlf=false archive --format=tar.gz --prefix=pbf-wastewater-rev1.2.1/ -o "$env:USERPROFILE\Downloads\pbf-wastewater-rev1.2.1.tar.gz" HEAD
```

`core.autocrlf=false` keeps Linux line endings in the `.sh` scripts. Without it, bash fails with errors like `$'\r': command not found`.

## 3. Upload to the VPS (Windows)

```powershell
scp "$env:USERPROFILE\Downloads\pbf-wastewater-rev1.2.1.tar.gz" ubuntu@51.161.52.12:/home/ubuntu/
```

## 4. Unpack and stage (on the VPS)

```powershell
ssh ubuntu@51.161.52.12
```

Then, on Ubuntu, unpack into a new dated folder so it doesn't mix with earlier uploads:

```bash
REL=~/ww-upgrade/$(date +%Y%m%d-%H%M)
mkdir -p "$REL"
tar xzf ~/pbf-wastewater-rev1.2.1.tar.gz -C "$REL"
cd "$REL/pbf-wastewater-rev1.2.1"
sudo bash stage.sh
```

`stage.sh` compiles `web.py`, runs the regression tests, and copies the code to `/opt/pbf-wastewater/rev1.2.1-stage/`. It also creates a separate staging settings database with all email switches **off**. Production keeps running and isn't touched.

If the tests fail, **stop here** and fix the code before deploying.

## 5. Preview staging in your browser

**a. Start staging on the VPS** (leave this SSH window open):

```bash
sudo -u pbf-wastewater env PBF_APP_DB=/var/lib/pbf-wastewater/dashboard-v1.2.1-stage.sqlite3 bash -c 'cd /opt/pbf-wastewater/rev1.2.1-stage && exec gunicorn --bind 127.0.0.1:8003 --workers 1 web:application'
```

**b. Open a tunnel from Windows** in a second PowerShell window:

```powershell
ssh -L 8003:127.0.0.1:8003 ubuntu@51.161.52.12
```

**c. Browse to http://localhost:8003** and sign in with the production `wastewater` login. Check that:

- The four top cards show icons, a green **● LIVE** pill and current values (EQ pH, EQ Tank Level, Discharge Flow, Total Flow).
- The values match production at https://51.161.52.12. Staging reads the same live history.
- The process diagram, Trends, Alarms and Reports pages load.

**d. Stop staging:** press **Ctrl+C** in the Gunicorn window, then close the tunnel window.

## 6. Deploy to production (on the VPS)

From the same release folder:

```bash
cd ~/ww-upgrade/<the dated folder>/pbf-wastewater-rev1.2.1
sudo bash deploy.sh --approved
```

The script:

1. Runs the checks and tests again.
2. Makes a **backup**: the current web code, the auth file, and verified copies of the history and settings databases.
3. Pauses the scheduler timer, installs the new code and restarts `pbf-wastewater-web`.
4. Confirms the app reads all four tags and that HTTP answers (an unauthenticated 401 is expected).
5. Restarts the timer.

If any step fails, it **restores the previous code automatically**.

**Write down the backup path it prints**, e.g. `/var/backups/pbf-wastewater/pre-deploy-1.2.1-20261117-1430`.

## 7. Verify production

On the VPS:

```bash
sudo systemctl is-active pbf-wastewater-web pbf-wastewater-collector.timer pbf-wastewater-scheduler.timer
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/api/summary   # expect 401
sudo journalctl -u pbf-wastewater-web -n 20 --no-pager
```

All three services should say `active`. Then open **https://51.161.52.12** in your browser, sign in, and check the new cards. The app sends `no-store` headers, but if the old look still shows, press **Ctrl+F5**.

## 8. Create or change the Operator login (once, optional)

There are two roles:

| | Admin (existing `wastewater` login) | Operator |
|---|---|---|
| View readings, trends, alarm list | ✔ | ✔ |
| Download CSV history (Reports) | ✔ | ✔ |
| Acknowledge alarms | ✔ | ✔ |
| View / change Settings | ✔ | ✘ (tab hidden, server returns 403) |
| View Audit log | ✔ | ✘ |

If an Operator account already exists (`/etc/pbf-wastewater/operator-auth.json`), it keeps working. To create one or change its password, run this on the VPS after deploying:

```bash
sudo python3 /opt/pbf-wastewater/web/web.py --set-operator operator
```

It asks for the password twice (at least 10 characters). The password is never typed on the command line. The file gets the same owner and group as `web-auth.json`, so the web service can read it. The change takes effect on the next sign-in; no restart is needed.

## Rollback

To put the previous web code back, using the backup path from step 6:

```bash
sudo bash rollback.sh /var/backups/pbf-wastewater/pre-deploy-1.2.1-YYYYMMDD-HHMMSS
```

This restores only the web code. History, settings, email config and the collector are left as they are.

## Notes

- **Revision number:** this release is still labelled **1.2.1**. `deploy.sh` checks that `web.py` reports `1.2.1`, so if you bump the revision, update it everywhere it appears: `web.py` (`summary()`), `deploy.sh`, `stage.sh`, `static/index.html` and `REVISION.txt`.
- **Not deployed:** `run_local.py`, `.vscode/` and `.local/` are local-development only. They may be in the package, but `deploy.sh` never installs them.
- **Cleanup (optional):** after a successful deploy you can remove old upload folders under `~/ww-upgrade/`. Keep the `/var/backups/pbf-wastewater/` backups until you're confident in the release.
- **Never** paste the dashboard, SMTP or Talk2M passwords into chat, commits or this file.
