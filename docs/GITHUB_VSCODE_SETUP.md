# VS Code + GitHub — Safe setup (Windows)

## 1. Extract the GitHub-ready ZIP

Open the **`pbf-wastewater-rev1.2.1`** folder in VS Code. This is the source folder, not the private VPS backup archive.

## 2. Confirm Git/Python

Use **Terminal → New Terminal** in VS Code (on Windows, this is usually PowerShell):

```powershell
git --version
py --version
py -m unittest test_revision -v
```

If Git is missing, install **Git for Windows** and restart VS Code. Python is needed only to run local code and tests.

## 3. Publish privately

1. Select **Source Control** (branch icon).
2. **Initialize Repository** if prompted.
3. Review the files carefully. Confirm `email.json`, `dmweb.json`, `web-auth.json`, database files, `.env`, backups, and password files are absent.
4. Stage the source files, enter commit message `Initial backup — Wastewater Dashboard Rev 1.2.1`, and **Commit**.
5. Click **Publish to GitHub**, sign in when prompted, and select **Private repository**.
6. Suggested repository name: `pbf-wastewater-dashboard`.
7. Visit GitHub to confirm the repository is **Private** and the source files are present.

If you prefer Git commands, in the **VS Code local terminal** use:

```powershell
git init -b main
git status --short
git add .
git diff --cached --name-only
git commit -m "Initial backup — Wastewater Dashboard Rev 1.2.1"
```

Then use VS Code **Publish to GitHub** and select **Private**.

## 4. Future change workflow

Edit → test → Source Control → review changed files → Commit → Sync/Push. Consider making feature branches before large changes; use tags like `v1.2.1` for release snapshots.

## 5. Critical security checklist

- Do not commit live backups, `.sqlite3` files, `.env`, any SMTP credentials, API keys, auth hashes, private keys, or exported dashboard customer data.
- A `.gitignore` helps, but it cannot protect secrets already committed. Review staged changes every time.
- Keep the repository private. GitHub-private is **not** a substitute for encrypted offline backup.
- Change VPS production using SSH and staging validation, not by saving files in VS Code alone.
- If a secret is accidentally pushed, revoke/rotate it; deleting a file in a later commit does not erase previous Git history.

## 6. Where the server code lives

Production: `/opt/pbf-wastewater/web/`; staging: `/opt/pbf-wastewater/rev1.2.53-stage/`.

Collector and scheduler are separate systemd units. Deploy scripts must be reviewed and tested on the current VPS before use. Back up the live system before every production upgrade.

### Official references

- VS Code: https://code.visualstudio.com/docs/sourcecontrol/repos-remotes
- VS Code/GitHub: https://code.visualstudio.com/docs/sourcecontrol/github
- GitHub: https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github
