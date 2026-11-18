<#
.SYNOPSIS
  Release and deploy the Wastewater Dashboard (follows docs/DEPLOY_STEP_BY_STEP.md).

.DESCRIPTION
  1. Bumps the revision (1.2.2 -> 1.2.3) in every file that carries it and adds release notes to REVISION.txt.
  2. Commits, tags and pushes to GitHub, then creates a GitHub release with the package attached.
  3. Uploads the package to the server, stages and tests it, optionally opens a preview,
     and, only after you type DEPLOY, puts it live and checks it.

  Easiest: double-click release.cmd. Or, in PowerShell from this folder:
      .\release.cmd
      .\release.cmd -Notes "Accounts page and password rules"
      .\release.cmd -DryRun          # show what would change, change nothing
      .\release.cmd -NoDeploy        # bump + GitHub only
      .\release.cmd -DeployOnly      # deploy the current version again (no bump)
      .\release.cmd -Version 1.3.0   # choose the new number yourself
#>
[CmdletBinding()]
param(
    [string]$Version,
    [string]$Notes,
    [switch]$NoDeploy,
    [switch]$DeployOnly,
    [switch]$LocalOnly,   # bump + commit + tag only: no push, no GitHub release, no deploy
    [switch]$DryRun,
    [switch]$Yes,         # answer "yes" to the routine questions (never to going live)
    [string]$Server = 'ubuntu@51.161.52.12'
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$HostIp = $Server.Split('@')[-1]

# Files whose current revision number is replaced. REVISION.txt is handled separately (history is kept).
$VersionFiles = @('web.py', 'test_revision.py', 'deploy.sh', 'stage.sh', 'static/index.html',
                  'README.md', 'README.txt', 'docs/DEPLOY_OVHCLOUD.md', 'docs/DEPLOY_STEP_BY_STEP.md',
                  'docs/GITHUB_VSCODE_SETUP.md')
$MustChange = @('web.py', 'test_revision.py', 'deploy.sh', 'stage.sh', 'static/index.html')

function Say([string]$t)  { Write-Host ''; Write-Host "=== $t" -ForegroundColor Cyan }
function Ok([string]$t)   { Write-Host "  OK   $t" -ForegroundColor Green }
function Warn([string]$t) { Write-Host "  !    $t" -ForegroundColor Yellow }
function Fail([string]$t) { Write-Host ''; Write-Host "STOPPED: $t" -ForegroundColor Red; exit 1 }
function Ask([string]$q)  { if ($Yes) { Write-Host "$q (y/N) y"; return $true }; return ((Read-Host "$q (y/N)") -match '^(y|yes)$') }
function Run([string]$exe, [string[]]$argv) {
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { Fail "'$exe $($argv -join ' ')' failed (exit code $LASTEXITCODE)." }
}
function ReadText([string]$path)  { [IO.File]::ReadAllText((Join-Path $PSScriptRoot $path), $Utf8) }
function WriteText([string]$path, [string]$text) { [IO.File]::WriteAllText((Join-Path $PSScriptRoot $path), $text, $Utf8) }

# ---------------------------------------------------------------- checks
Say 'Checking this computer'
foreach ($tool in @('git', 'ssh', 'scp')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Fail "'$tool' is not installed or not on PATH." }
}
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne 'main') { Fail "You are on branch '$branch'. Switch to 'main' first." }
Ok 'git, ssh and scp found; on branch main'

$m = [regex]::Match((ReadText 'web.py'), "'revision':'(\d+)\.(\d+)\.(\d+)'")
if (-not $m.Success) { Fail 'Could not find the revision number in web.py.' }
$Current = '{0}.{1}.{2}' -f $m.Groups[1].Value, $m.Groups[2].Value, $m.Groups[3].Value

if ($DeployOnly) {
    $New = $Current
} elseif ($Version) {
    if ($Version -notmatch '^\d+\.\d+\.\d+$') { Fail "Version must look like 1.2.3 (got '$Version')." }
    $New = $Version
} else {
    $New = '{0}.{1}.{2}' -f $m.Groups[1].Value, $m.Groups[2].Value, ([int]$m.Groups[3].Value + 1)
}

$PkgName = "pbf-wastewater-rev$New.tar.gz"
$Pkg = Join-Path $env:USERPROFILE "Downloads\$PkgName"

# ---------------------------------------------------------------- 1. bump revision
if (-not $DeployOnly) {
    Say "Step 1: Update revision $Current -> $New"
    if ($New -eq $Current) { Fail 'The new version is the same as the current one. Use -DeployOnly to redeploy.' }
    if (git tag -l $New) { Fail "A release '$New' already exists in git. Choose another number with -Version." }

    $pattern = '(?<![\d.])' + [regex]::Escape($Current) + '(?!\d)'
    $updates = @{}
    foreach ($f in $VersionFiles) {
        if (-not (Test-Path -LiteralPath $f)) { continue }
        $text = ReadText $f
        $count = [regex]::Matches($text, $pattern).Count
        if ($count -gt 0) { $updates[$f] = [regex]::Replace($text, $pattern, $New); Write-Host ("  {0,-32} {1} place(s)" -f $f, $count) }
    }
    foreach ($f in $MustChange) { if (-not $updates.ContainsKey($f)) { Fail "$f does not contain $Current, so the files are out of step. Nothing was changed." } }

    # Release notes: commits since the revision number last changed in web.py
    $base = (git log -1 --format=%H "-S'revision':'$Current'" -- web.py)
    if ($base) { $log = @(git log --no-merges "--format=- %s" "$base..HEAD") } else { $log = @(git log --no-merges -10 "--format=- %s") }
    $pending = @(git status --porcelain)
    if (-not $Notes -and -not $Yes -and -not $DryRun) { $Notes = Read-Host 'Describe this release in one line (or press Enter to skip)' }
    $noteLines = @()
    if ($Notes) { $noteLines += "- $Notes" }
    $noteLines += $log
    if ($pending.Count -gt 0) { $noteLines += '- Uncommitted work included in this release (see the commit for details).' }
    if ($noteLines.Count -eq 0) { $noteLines = @('- Maintenance release.') }
    $NotesText = "Changes since Rev ${Current}:`r`n" + ($noteLines -join "`r`n")

    Write-Host ''
    Write-Host $NotesText
    if ($pending.Count -gt 0) { Write-Host ''; Write-Host 'Uncommitted changes that will be included:'; $pending | ForEach-Object { Write-Host "  $_" } }

    if ($DryRun) { Say 'Dry run: nothing was changed.'; exit 0 }
    if (-not (Ask "Update the files to ${New}?")) { Fail 'Cancelled. Nothing was changed.' }

    foreach ($f in $updates.Keys) { WriteText $f $updates[$f] }
    $rev = ReadText 'REVISION.txt'
    $header = "Revision $New - Wastewater Dashboard`r`nDesign and program by Daniel Salazar.`r`nReleased $(Get-Date -Format 'yyyy-MM-dd').`r`n`r`n$NotesText`r`n`r`n" + ('-' * 72) + "`r`n`r`n"
    WriteText 'REVISION.txt' ($header + $rev)
    Ok "Revision $New written to $($updates.Count + 1) files"

    # ------------------------------------------------------------ 2. commit, tag, push, GitHub release
    Say "Step 2: Save to git and GitHub as release $New"
    Run git @('add', '-A')
    $risky = @(git diff --cached --name-only | Where-Object { $_ -match '\.(sqlite3?|db)$|auth\.json$|email\.json$|dmweb\.json$|(^|/)\.env' })
    if ($risky.Count -gt 0) { git reset -q; Fail "These private files were about to be committed: $($risky -join ', '). Nothing was committed." }
    $msg = @('commit', '-q', '-m', "Release $New")
    if ($Notes) { $msg += @('-m', $Notes) }
    Run git $msg
    Run git @('tag', '-a', $New, '-m', "Rev $New")
    Ok "Committed and tagged $New"
}

# Package (built from the committed code; Linux line endings kept for the .sh scripts)
Say "Building package $PkgName"
if ($DeployOnly -and @(git status --porcelain).Count -gt 0) { Warn 'You have uncommitted changes. They are NOT in this package.' }
Run git @('-c', 'core.autocrlf=false', 'archive', '--format=tar.gz', "--prefix=pbf-wastewater-rev$New/", '-o', $Pkg, 'HEAD')
$Hash = (Get-FileHash -LiteralPath $Pkg -Algorithm SHA256).Hash.ToLower()
Ok "$Pkg"
Ok "SHA-256 $Hash"

if (-not $DeployOnly -and -not $LocalOnly) {
    $pushed = $true
    & git push origin main;  if ($LASTEXITCODE -ne 0) { $pushed = $false }
    if ($pushed) { & git push origin $New; if ($LASTEXITCODE -ne 0) { $pushed = $false } }
    if ($pushed) { Ok 'Pushed to GitHub' } else { Warn 'Could not push to GitHub. Run later:  git push origin main --follow-tags' }

    $notesFile = Join-Path $env:TEMP "wastewater-release-$New.txt"
    [IO.File]::WriteAllText($notesFile, $NotesText, $Utf8)
    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if (-not $pushed) {
        Warn 'GitHub release skipped because the push failed.'
    } elseif (-not $gh) {
        Warn 'GitHub CLI (gh) is not installed, so the GitHub release was not created. To set it up once:'
        Write-Host '         winget install --id GitHub.cli     (then close and reopen PowerShell)'
        Write-Host '         gh auth login'
        Write-Host "       Then create this release:  gh release create $New `"$Pkg`" --title `"Rev $New`" --notes-file `"$notesFile`""
    } else {
        # PowerShell 5.1 turns a native program's stderr into a terminating error under
        # ErrorActionPreference=Stop, and 'gh auth status' writes to stderr, so relax it here.
        $ErrorActionPreference = 'Continue'
        & gh auth status 2>&1 | Out-Null
        $ghSignedIn = ($LASTEXITCODE -eq 0)
        $ErrorActionPreference = 'Stop'
        if (-not $ghSignedIn) { Warn 'GitHub CLI is not signed in. Run:  gh auth login   then re-run the gh release command below.'
            Write-Host "       gh release create $New `"$Pkg`" --title `"Rev $New`" --notes-file `"$notesFile`"" }
        else {
            & gh release create $New $Pkg --title "Rev $New" --notes-file $notesFile
            if ($LASTEXITCODE -eq 0) { Ok "GitHub release $New created" } else { Warn 'GitHub release failed (the code is pushed; you can create the release on github.com).' }
        }
    }
}

if ($NoDeploy -or $LocalOnly) { Say "Done. Release $New is ready but NOT deployed. Deploy later with:  .\release.cmd -DeployOnly"; exit 0 }

# ---------------------------------------------------------------- 3. deploy (DEPLOY_STEP_BY_STEP.md steps 4-10)
$Rel = "~/ww-upgrade/rev$New"
$Dir = "$Rel/pbf-wastewater-rev$New"

Say "Step 3: Upload to the server $HostIp"
Write-Host '  If asked, type the server password (nothing shows while typing).'
Run scp @($Pkg, "${Server}:/home/ubuntu/")
Ok 'Uploaded'

Say 'Step 4: Install a TEST copy and run the tests on the server (live site not touched)'
$stageCmd = "if sha256sum ~/$PkgName | grep -q ^$Hash; then cd ~ && sudo rm -rf $Rel && mkdir -p $Rel && tar xzf ~/$PkgName -C $Rel && cd $Dir && sed -i 's/\r$//' *.sh && sudo bash stage.sh; else echo WRONG-FILE-NOTHING-INSTALLED; exit 3; fi"
& ssh -t $Server $stageCmd
if ($LASTEXITCODE -eq 3) { Fail 'The file on the server does not match this package. Nothing was installed.' }
if ($LASTEXITCODE -ne 0) { Fail 'Staging or tests failed on the server. The live dashboard was NOT changed.' }
Ok 'Test copy installed and tests passed'

if (Ask 'Open the test copy in your browser to check it first? (recommended)') {
    $gun = "sudo -u pbf-wastewater env PBF_APP_DB=/var/lib/pbf-wastewater/dashboard-v$New-stage.sqlite3 timeout 1800 bash -c 'cd /opt/pbf-wastewater/rev$New-stage && exec gunicorn --bind 127.0.0.1:8003 --workers 1 web:application'"
    $preview = Start-Process -FilePath 'ssh' -ArgumentList ('-t -L 8003:127.0.0.1:8003 ' + $Server + ' "' + $gun + '"') -PassThru
    Write-Host '  A second window opened. Type the server password there if asked.'
    Read-Host '  When that window shows "Listening at", press Enter here to open the browser'
    Start-Process 'http://localhost:8003'
    Write-Host "  Sign in as Admin and check: four boxes LIVE, 'Rev $New' top-right, Trends/Alarms/Reports/Settings open."
    Read-Host '  When you have finished checking, press Enter here to close the test copy'
    if (-not $preview.HasExited) { Stop-Process -Id $preview.Id -Force -ErrorAction SilentlyContinue }
    Ok 'Test copy closed'
}

Say "Step 5: Put Rev $New LIVE on https://$HostIp"
Write-Host '  The server backs up the current version first and restores it automatically if anything fails.'
if ((Read-Host "  Type DEPLOY to go live (anything else stops here)") -cne 'DEPLOY') {
    Say "Stopped before going live. The test copy is on the server; deploy later with:  .\release.cmd -DeployOnly"; exit 0
}
$deployCmd = "cd $Dir && sudo bash deploy.sh --approved && echo && echo ===== CHECKS ===== && sudo systemctl is-active pbf-wastewater-web pbf-wastewater-collector.timer pbf-wastewater-scheduler.timer pbf-wastewater-backup.timer && curl -s -o /dev/null -w 'HTTP check: %{http_code} (401 expected)\n' http://127.0.0.1:8001/api/summary && echo && echo SAVE THIS BACKUP PATH: && sudo sh -c 'ls -1dt /var/backups/pbf-wastewater/pre-deploy-$New-* | head -1' | tee -a ~/ww-upgrade/deploy-history.txt"
& ssh -t $Server $deployCmd
if ($LASTEXITCODE -ne 0) {
    Fail 'Deployment did not complete. If deploy.sh printed "restoring previous dashboard code", the old version is running again. Copy the window text and ask for help.'
}

Say "Rev $New is LIVE"
Write-Host '  1. Copy the BACKUP PATH above into Notepad. You need it to roll back (see DEPLOY_STEP_BY_STEP.md).'
Write-Host '     (It is also saved on the server in ~/ww-upgrade/deploy-history.txt)'
Write-Host "  2. Your browser opens https://$HostIp now. Check 'Rev $New' top-right (press Ctrl+F5 if it looks old)."
Start-Process "https://$HostIp"
