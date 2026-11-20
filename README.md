# Wastewater Dashboard — Rev 1.2.35

**Design and program by Daniel Salazar**

Private source-code repository for an industrial wastewater dashboard.

## System architecture

Ewon Flexy 205 → Talk2M DataMailbox API → VPS collector → SQLite history → Python/Gunicorn web application → Dashboard and email scheduler.

This repo contains **application source**, interface artwork, deployment scripts, scheduler units, and regression tests. It does **not** include live SQLite history, SMTP passwords, API keys, authentication hashes, or private VPS backups.

## Four monitored tags

| Tag | Display | Unit |
| --- | --- | --- |
| `pH_Reading` | EQ pH | pH |
| `EQ_Tank_Level_Ft` | EQ Tank Level | ft |
| `FLOW_RATE` | Discharge Flow | L/min (raw US gal/min × 3.78541) |
| `Total_Flow_Li` | Total Flow | L |

## Application features

- Live process overview with readings over the EQ tank process diagram.
- Separate Trends and Events/Alarms pages.
- Excel reports (Total_Flow_Li EOD summary with daily usage), configurable pH, EQ high-level, no-flow and data-stopped alarms.
- Daily/monthly/quarterly email reports (Excel attached), named Operator logins, sign-in lockout and audit log.
- Nightly database backups on the server.
- Python WSGI application; no live instrumentation is included in this repository.

## Working locally in VS Code

1. Install VS Code, Git, and Python 3.11+.
2. Open this folder in VS Code: **File → Open Folder**.
3. From the VS Code terminal, run `python -m unittest test_revision -v` (or `py -m unittest test_revision -v` on Windows).
4. To publish, open **Source Control → Initialize Repository → Commit**, then **Publish to GitHub → Private**.

### Run the dashboard locally

```
py -m pip install tzdata   # once, on Windows (time-zone data)
py run_local.py            # then open http://127.0.0.1:8000  (Admin: local / local, Operator: operator / operator)
py run_local.py --reset    # regenerate demo data
py run_local.py --set-admin local        # change the local Admin password (hidden prompt)
py run_local.py --set-operator operator  # change the local Operator password
py run_local.py --db C:\path\to\history-copy.sqlite3   # view a copied history DB instead
```

Or press **F5** in VS Code ("Run dashboard locally"). All local data lives in the git-ignored `.local/` folder: 7 days of demo history plus a new sample every 30 s. Email never sends, and nothing contacts the VPS.

Tests use temporary databases and do not contact the VPS or SMTP. Running the actual dashboard needs a controlled test environment and proper configuration; do not place real credentials in the repository.

## Live VPS deployment

Production paths (on the Ubuntu VPS):

- `/opt/pbf-wastewater/web/` — web app and static UI
- `/var/lib/pbf-wastewater/history.sqlite3` — live collector history (read-only from web app)
- `/var/lib/pbf-wastewater/dashboard-v1.2.sqlite3` — settings/events/audit metadata
- `/etc/pbf-wastewater/web-auth.json` — Basic Auth (private)
- `/etc/pbf-wastewater/email.json` — SMTP configuration (private)
- `/var/backups/pbf-wastewater-nightly/` — nightly database backups (newest 14)
- `pbf-wastewater-web.service`, `pbf-wastewater-scheduler.timer` and `pbf-wastewater-backup.timer`

**Do not run `deploy.sh` from Windows.** Upload an approved release to the VPS, SSH into Ubuntu, and stage/test before running the explicit production deployment. Take a consistent database backup first.

**Release and deploy in one go:** double-click `release.cmd` (or run `.\release.cmd -DryRun` first to preview). It bumps the revision, commits, tags, pushes, creates the GitHub release, and stages and deploys to the VPS after you type `DEPLOY`.

**Copy the newest nightly backup to this PC:** double-click `pull-backups.cmd` (weekly). The server backs up both databases every night at 02:30 and keeps 14.

**Add or change a login on the live server:** double-click `set-login.cmd` (or `.\set-login.cmd -Show` to only list the current logins). It detects the server's revision, runs the right command, and asks for the new password in a hidden prompt.

Step-by-step update procedure: [plain-language guide](docs/DEPLOY_STEP_BY_STEP.md) (no programming needed) or [technical guide](docs/DEPLOY_OVHCLOUD.md).

See [GitHub/VS Code setup](docs/GITHUB_VSCODE_SETUP.md), [release notes](REVISION.txt), and [original deployment notes](README.txt).

## Backup policy

GitHub is a source-code version history, **not a complete recovery backup**. Save encrypted copies of live SQLite databases, private configuration, and the VPS system service files separately in secure offline storage. Never upload the full recovery ZIP or encryption keys to GitHub.
