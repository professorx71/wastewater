# Deploying an update to the OVHcloud VPS (51.161.52.12)

This updates the **existing** production dashboard at https://51.161.52.12. It replaces only the web code (`web.py`, `static/`, `REVISION.txt`). It does **not** change the Talk2M collector, history database, settings/email metadata, SMTP config or passwords.

The workflow is: **package on Windows → upload → stage and preview → deploy → verify**. If anything looks wrong, roll back (see **Rollback** below). For a plain-language version, see [DEPLOY_STEP_BY_STEP.md](DEPLOY_STEP_BY_STEP.md).

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
git -c core.autocrlf=false archive --format=tar.gz --prefix=pbf-wastewater-rev1.2.13/ -o "$env:USERPROFILE\Downloads\pbf-wastewater-rev1.2.13.tar.gz" HEAD
```

`core.autocrlf=false` keeps Linux line endings in the `.sh` scripts. Without it, bash fails with errors like `$'\r': command not found`.

## 3. Upload to the VPS (Windows)

```powershell
scp "$env:USERPROFILE\Downloads\pbf-wastewater-rev1.2.13.tar.gz" ubuntu@51.161.52.12:/home/ubuntu/
```

## 4. Unpack and stage (on the VPS)

```powershell
ssh ubuntu@51.161.52.12
```

Then, on Ubuntu, unpack into a new dated folder so it doesn't mix with earlier uploads:

```bash
REL=~/ww-upgrade/$(date +%Y%m%d-%H%M)
mkdir -p "$REL"
tar xzf ~/pbf-wastewater-rev1.2.13.tar.gz -C "$REL"
cd "$REL/pbf-wastewater-rev1.2.13"
sudo bash stage.sh
```

`stage.sh` compiles `web.py`, runs the regression tests, and copies the code to `/opt/pbf-wastewater/rev1.2.13-stage/`. It also creates a separate staging settings database with all email switches **off**. Production keeps running and isn't touched.

If the tests fail, **stop here** and fix the code before deploying.

## 5. Preview staging in your browser

**a. Start staging on the VPS** (leave this SSH window open):

```bash
sudo -u pbf-wastewater env PBF_APP_DB=/var/lib/pbf-wastewater/dashboard-v1.2.13-stage.sqlite3 bash -c 'cd /opt/pbf-wastewater/rev1.2.13-stage && exec gunicorn --bind 127.0.0.1:8003 --workers 1 web:application'
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
cd ~/ww-upgrade/<the dated folder>/pbf-wastewater-rev1.2.13
sudo bash deploy.sh --approved
```

The script:

1. Runs the checks and tests again.
2. Makes a **backup**: the current web code, the auth file, and verified copies of the history and settings databases.
3. Pauses the scheduler timer, installs the new code and restarts `pbf-wastewater-web`.
4. Confirms the app reads all four tags and that HTTP answers (an unauthenticated 401 is expected).
5. Restarts the timer.

If any step fails, it **restores the previous code automatically**.

**Write down the backup path it prints**, e.g. `/var/backups/pbf-wastewater/pre-deploy-1.2.13-20261117-1430`.

## 7. Verify production

On the VPS:

```bash
sudo systemctl is-active pbf-wastewater-web pbf-wastewater-collector.timer pbf-wastewater-scheduler.timer pbf-wastewater-backup.timer
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/api/summary   # expect 401
sudo journalctl -u pbf-wastewater-web -n 20 --no-pager
```

All four should say `active`. `pbf-wastewater-backup.timer` is installed by `deploy.sh` from 1.2.13 on. Then open **https://51.161.52.12** in your browser, sign in, and check the new cards. The app sends `no-store` headers, but if the old look still shows, press **Ctrl+F5**.

## 8. Admin and Operator logins

There are two roles:

| | Admin (existing `wastewater` login) | Operator |
|---|---|---|
| View readings, trends, alarm list | ✔ | ✔ |
| Download Excel reports (Reports) | ✔ | ✔ |
| Acknowledge alarms | ✔ | ✔ |
| View / change Settings | ✔ | ✘ (tab hidden, server returns 403) |
| View Audit log | ✔ | ✘ |
| Change Admin password, add / reset / remove Operator logins | ✔ | ✘ |

**From the dashboard (normal way):** sign in as Admin and go to **Settings → Accounts and passwords**.

- **Admin login:** enter the current password, then the new one twice. You'll be asked to sign in again.
- **Operator logins:** give each person their own login, so the Audit log and alarm acknowledgements show who did it. Enter a username and a password (twice) and click **Add or reset Operator**; using an existing username resets that person's password. **Remove** takes one person off; **Turn off all Operator logins** blocks every Operator.
- **Lockout:** after 10 *different* wrong passwords from one place within 15 minutes, sign-in from there is refused for 15 minutes (logged in Audit as *Sign-in locked*). Repeating the same wrong password, for example an old browser tab after a password change, counts once.

Logins saved this way are stored (as salted PBKDF2 hashes) in the dashboard settings database, `/var/lib/pbf-wastewater/dashboard-v1.2.sqlite3`. `deploy.sh` already backs that database up. They take priority over the original `/etc/pbf-wastewater/web-auth.json` and `operator-auth.json` files, which keep working until a login is changed from the page. Every change is written to the Audit log, without the password.

**From the server (if you're locked out of Admin):**

```bash
sudo -u pbf-wastewater python3 /opt/pbf-wastewater/web/web.py --set-admin wastewater
sudo -u pbf-wastewater python3 /opt/pbf-wastewater/web/web.py --set-operator operator
```

`--set-operator NAME` adds that Operator, or resets their password if they already exist.

Each command asks for the password twice (Admin at least 6 characters, Operator at least 4), so the password is never typed on the command line. It takes effect at the next sign-in; no restart is needed.

> If you **roll back** to a version before this feature, logins changed from the page stop applying, and the original `/etc/pbf-wastewater/*-auth.json` passwords work again.

### HTTPS proxy: pass the visitor's address

The lockout works per visitor address. Behind the HTTPS proxy, the dashboard sees every visitor as `127.0.0.1` unless the proxy passes the real address; without it, lockout falls back to per-username. In the nginx `location` block that proxies to `127.0.0.1:8001`, add:

```nginx
proxy_set_header X-Real-IP $remote_addr;
```

then `sudo nginx -t && sudo systemctl reload nginx`.

## 9. Nightly backups

`deploy.sh` installs `pbf-wastewater-backup.timer`. Every night at 02:30 it writes consistent copies of `history.sqlite3` and the dashboard database to `/var/backups/pbf-wastewater-nightly/YYYYMMDD-HHMMSS/` and keeps the newest 14.

```bash
sudo systemctl list-timers pbf-wastewater-backup.timer       # next run
sudo systemctl start pbf-wastewater-backup.service           # run one now
sudo ls /var/backups/pbf-wastewater-nightly/
```

**Keep a copy off the server.** On the Windows PC, double-click `pull-backups.cmd` once a week. It downloads the newest nightly backup to `Documents\Wastewater-Backups` (or `-Dest D:\Somewhere`). The files contain login hashes, so keep them private.

**Restore** (only if the database is damaged): stop the web, scheduler and collector, copy the two files back over `/var/lib/pbf-wastewater/history.sqlite3` and `dashboard-v1.2.sqlite3` (owner `pbf-wastewater`), then start them again.

## Rollback

To put the previous web code back, using the backup path from step 6:

```bash
sudo bash rollback.sh /var/backups/pbf-wastewater/pre-deploy-1.2.13-YYYYMMDD-HHMMSS
```

This restores only the web code. History, settings, email config and the collector are left as they are.

## Notes

- **Revision number:** this release is **1.2.13**. `deploy.sh` checks that `web.py` reports `1.2.13`, so for the next release update the number everywhere it appears: `web.py` (`summary()`), `test_revision.py`, `deploy.sh`, `stage.sh`, `static/index.html`, `REVISION.txt`, `README.md`/`README.txt` and this guide.
- **Not deployed:** `run_local.py`, `.vscode/` and `.local/` are local-development only. They may be in the package, but `deploy.sh` never installs them.
- **Cleanup (optional):** after a successful deploy you can remove old upload folders under `~/ww-upgrade/`. Keep the `/var/backups/pbf-wastewater/` backups until you're confident in the release.
- **Never** paste the dashboard, SMTP or Talk2M passwords into chat, commits or this file.
