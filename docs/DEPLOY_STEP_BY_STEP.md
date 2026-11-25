# How to put the new dashboard version on the server — plain step-by-step

This guide updates the live dashboard at **https://51.161.52.12** to **version 1.2.62**. You don't need to know programming. You will **copy commands from this page and paste them** into a window, then check that what you see matches what this guide says.

**Time needed:** about 30 minutes.
**What stays safe:** the readings history, alarm settings, email settings and passwords are **not** changed. The server makes a backup automatically before changing anything.

---

## ⚡ The quick way: one script does all the steps below

In the project folder `C:\developer\wastewater`, **double-click `release.cmd`**. Or, in the VS Code terminal, type:

```powershell
.\release.cmd
```

It does everything on this page for you and asks before each important step:

1. **Raises the version number** by one (for example 1.4.7 → 1.4.8) in every file and adds release notes to `REVISION.txt`.
2. **Saves to GitHub**: commits, creates the release tag, pushes, and creates a GitHub release with the package attached.
3. **Uploads** the package to the server and checks it arrived intact.
4. **Installs a test copy** and runs the tests. The live site isn't touched.
5. **Offers a preview** of the test copy in your browser.
6. **Goes live only when you type `DEPLOY`**, then checks the services and shows the **backup path**. Save that path.

Type the server password whenever it's asked for (nothing shows while you type).

| You want to… | Type |
|---|---|
| See what would change, without changing anything | `.\release.cmd -DryRun` |
| Add a one-line description to the release | `.\release.cmd -Notes "What changed"` |
| Release to GitHub but not deploy yet | `.\release.cmd -NoDeploy` |
| Deploy the current version again (no new number) | `.\release.cmd -DeployOnly` |
| Choose the version number yourself | `.\release.cmd -Version 1.3.0` |

**GitHub releases need the GitHub CLI, installed once.** Run `winget install --id GitHub.cli`, close and reopen the terminal, then run `gh auth login`. Without it, the script still saves and pushes to GitHub, and prints the command to create the release later.

If the script stops with **STOPPED:** in red, read the message. Nothing after that point was done. The rest of this page shows the same steps done by hand.

---

## Before you start: things to know

**Copying and pasting**

- Each grey box below is one command. Copy the whole box (there's a copy button in the top-right corner of each box), paste it into the window, and press **Enter**.
- To paste into a **PowerShell** window, right-click or press **Ctrl+V**.
- To paste into the **server** window (after step 4), right-click. Ctrl+V may not work there.

**Typing passwords**

- When you type a password, **nothing appears on screen**: no dots, no stars. That's normal. Type it and press **Enter**.

**Have these ready**

- The **server password** for the user `ubuntu`, if the server asks for one.
- The **dashboard Admin password** for the user `wastewater`.

**If something goes wrong**

- If anything shows red text with words like **error**, **failed** or **denied**, **stop**. Take a screenshot and ask for help. Don't try to fix it by guessing.

---

## Part A — On your Windows computer

### Step 1. Open PowerShell in the project folder

1. Open **VS Code**. It should show the `wastewater` project.
2. At the top menu, click **Terminal → New Terminal**. A window opens at the bottom. This is **PowerShell**.
3. Paste this and press Enter:

```powershell
cd C:\developer\wastewater
```

### Step 2. Save the changes into the project history

Paste this and press Enter:

```powershell
git add -A
git commit -m "Release 1.2.62"
```

✅ **You should see** a line like `[main 1a2b3c4] Release 1.2.62` and a count of files changed.
ℹ️ If it says `nothing to commit`, that's fine. The changes were already saved. Continue.

### Step 3. Build the upload package

Paste this as **one line** and press Enter:

```powershell
git -c core.autocrlf=false archive --format=tar.gz --prefix=pbf-wastewater-rev1.2.62/ -o "$env:USERPROFILE\Downloads\pbf-wastewater-rev1.2.62.tar.gz" HEAD
```

✅ **You should see** nothing, just a new empty line. That means it worked. The file `pbf-wastewater-rev1.2.62.tar.gz` is now in your **Downloads** folder.

### Step 4. Send the package to the server

Paste this and press Enter:

```powershell
scp "$env:USERPROFILE\Downloads\pbf-wastewater-rev1.2.62.tar.gz" ubuntu@51.161.52.12:/home/ubuntu/
```

- If it asks **"Are you sure you want to continue connecting (yes/no)?"**, type `yes` and press Enter.
- If it asks for a **password**, type the **server password** (it won't show) and press Enter.

✅ **You should see** a line ending in **100%**.

---

## Part B — On the server

### Step 5. Connect to the server

In the same PowerShell window, paste:

```powershell
ssh ubuntu@51.161.52.12
```

Enter the server password if asked.

✅ **You should see** a welcome message, and the line where you type now starts with something like `ubuntu@...:~$`. **You are now typing on the server, not on your computer.**

### Step 6. Unpack the package

Paste this whole box and press Enter:

```bash
cd ~
sudo rm -rf ~/ww-upgrade/rev1.2.62
mkdir -p ~/ww-upgrade/rev1.2.62
tar xzf ~/pbf-wastewater-rev1.2.62.tar.gz -C ~/ww-upgrade/rev1.2.62
cd ~/ww-upgrade/rev1.2.62/pbf-wastewater-rev1.2.62
ls
```

✅ **You should see** a list of names that includes `deploy.sh`, `stage.sh`, `web.py` and `static`.

### Step 7. Install a test copy and check it

This puts a **test copy** beside the live dashboard. The live dashboard keeps running normally.

```bash
sudo bash stage.sh
```

If it asks for a password, type the **server password**.

✅ **You should see** `OK` near the top, then **"Staging installed. Production remains untouched."**
🛑 If you see **FAILED** or **Error**, stop here and ask for help. Nothing live has been changed.

### Step 8. Look at the test copy in your browser

This step uses **two windows**.

**8a. Start the test copy.** In the **server window** you already have open, paste:

```bash
sudo -u pbf-wastewater env PBF_APP_DB=/var/lib/pbf-wastewater/dashboard-v1.2.62-stage.sqlite3 bash -c 'cd /opt/pbf-wastewater/rev1.2.62-stage && exec gunicorn --bind 127.0.0.1:8003 --workers 1 web:application'
```

✅ You should see a few lines including **"Listening at: http://127.0.0.1:8003"**. **Leave this window alone.** It stays busy while the test copy runs.

**8b. Open a second window.** In VS Code, click the **+** at the top-right of the terminal panel to open a new PowerShell. Paste:

```powershell
ssh -L 8003:127.0.0.1:8003 ubuntu@51.161.52.12
```

Enter the server password if asked. Leave this window open too.

**8c. Open the test copy.** In your web browser, go to:

**http://localhost:8003**

Sign in with the **Admin** login (`wastewater` and its password). Check that:

- [ ] The four boxes at the top have **pictures**, a green **● LIVE** label and numbers
- [ ] The numbers are about the same as on the live site, https://51.161.52.12
- [ ] The top-right corner shows **Admin**, **Rev 1.2.62** and a **Switch user** button
- [ ] **Trends**, **Alarms**, **Reports** and **Settings** pages open

🛑 If something looks wrong, skip to **"If you want to stop"** at the bottom. Nothing live has been changed yet.

**8d. Close the test copy.**

1. Close the browser tab.
2. In the **first** window (8a), press **Ctrl+C**. You're back at the `ubuntu@...:~$` line.
3. In the **second** window (8b), type `exit` and press Enter.

### Step 9. Put the new version live

In the **first** server window, paste:

```bash
cd ~/ww-upgrade/rev1.2.62/pbf-wastewater-rev1.2.62
sudo bash deploy.sh --approved
```

Wait about a minute. The live dashboard is offline for a few seconds during this step.

✅ **You should see** at the end:
- `Revision 1.2.62 deployed. Backup: /var/backups/pbf-wastewater/pre-deploy-1.2.62-...`

📝 **Copy that whole Backup line and save it** in Notepad or an email to yourself. You need it if you ever want to go back to the old version.

ℹ️ If you see **"Production deployment failed — restoring previous dashboard code"**, the server put the old version back automatically and the live dashboard still works. Stop and ask for help.

### Step 10. Check that everything is running

Paste:

```bash
sudo systemctl is-active pbf-wastewater-web pbf-wastewater-collector.timer pbf-wastewater-scheduler.timer pbf-wastewater-backup.timer
```

✅ **You should see** `active` four times. The fourth is the nightly backup.

Then, in your browser, open **https://51.161.52.12** and sign in. Check that you see the new boxes and **Rev 1.2.62** in the top-right corner. If it still looks old, press **Ctrl+F5**.

### Step 11 (optional). Set up passwords

You do this in the **dashboard itself**, not in the server window.

1. Open **https://51.161.52.12** and sign in as **Admin**.
2. Click **Settings** and scroll down to **Accounts and passwords**.
3. **To change the Admin password:** type the current password, then the new password twice, and click **Change Admin login**. The browser asks you to sign in again. Use the new password.
4. **To create a login for a staff member (Operator):** type their username (for example `anna`), type a password twice, and click **Add or reset Operator**. Give each person their own login. Operators can see readings, download reports and acknowledge alarms, but cannot change settings.
5. **To remove one person,** click **Remove** next to their name. **To stop all Operators signing in,** click **Turn off all Operator logins**.

Passwords must be at least **6 characters for Admin** and **4 characters for Operator**.

✅ **You should see** a green message such as `Operator login saved for operator`.

**Forgot the Admin password?** Connect to the server (step 5) and paste the line below. Type a new password twice when asked:

```bash
sudo -u pbf-wastewater python3 /opt/pbf-wastewater/web/web.py --set-admin wastewater
```

### Step 12 (weekly). Copy a backup to your computer

The server backs up its data every night. Once a week, on your computer, double-click **`pull-backups.cmd`** in `C:\developer\wastewater`. It saves the newest backup in **Documents → Wastewater-Backups**.

### Step 13. Disconnect from the server

```bash
exit
```

**Done!** 🎉

---

## If you want to stop

- **Before step 9:** nothing live has been changed. Just close the windows.
- **After step 9, to go back to the old version:** connect to the server (step 5), then paste the line below. Replace the part after `rollback.sh` with the **Backup** path you saved in step 9.

```bash
cd ~/ww-upgrade/rev1.2.62/pbf-wastewater-rev1.2.62
sudo bash rollback.sh /var/backups/pbf-wastewater/pre-deploy-1.2.62-PASTE-YOUR-DATE-HERE
```

✅ **You should see** "Previous web code restored". Readings history and settings are not affected.

---

**Never** paste passwords into emails, chat messages or this document.
