<#
.SYNOPSIS
  Copy the newest nightly database backup from the live server to this PC.

.DESCRIPTION
  The server makes a backup every night at 02:30 (keeps the newest 14) in
  /var/backups/pbf-wastewater-nightly. This copies the newest one to a folder on this PC
  so a copy exists OFF the server. Run it once a week (double-click pull-backups.cmd).

  The backup contains the readings history, settings, alarms, audit log and login hashes
  (not plain passwords). Keep the folder private, for example on an encrypted drive.

      .\pull-backups.cmd
      .\pull-backups.cmd -Dest D:\Backups\Wastewater
#>
[CmdletBinding()]
param(
    [string]$Dest = (Join-Path $env:USERPROFILE 'Documents\Wastewater-Backups'),
    [string]$Server = 'ubuntu@51.161.52.12'
)
$ErrorActionPreference = 'Continue'
function Fail([string]$t) { Write-Host ''; Write-Host "STOPPED: $t" -ForegroundColor Red; exit 1 }

foreach ($tool in @('ssh', 'scp')) { if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Fail "'$tool' is not installed." } }
New-Item -ItemType Directory -Force -Path $Dest | Out-Null

# Runs on the server (bash). Packs the newest nightly folder into /tmp for download.
$Remote = @'
set -e
L=$(sudo -n find /var/backups/pbf-wastewater-nightly -mindepth 1 -maxdepth 1 -type d | sort | tail -1)
if [ -z "$L" ]; then echo NOBACKUP; exit 3; fi
sudo -n tar -C "$L" -czf /tmp/ww-nightly.tgz .
sudo -n chown "$(id -un)" /tmp/ww-nightly.tgz
echo "NAME=$(basename "$L")"
'@

Write-Host '=== Packing the newest nightly backup on the server' -ForegroundColor Cyan
Write-Host '  If asked, type the SERVER password (nothing shows while typing).'
$out = $Remote | & ssh $Server "tr -d '\r' | bash -s" 2>&1
$name = ($out | Where-Object { "$_" -like 'NAME=*' } | Select-Object -First 1)
if ("$out" -match 'NOBACKUP') { Fail 'No nightly backup on the server yet. The first one runs at 02:30 after deploying 1.2.9 or later.' }
if (-not $name) { $out | ForEach-Object { Write-Host "  $_" }; Fail 'Could not pack the backup on the server.' }
$name = "$name".Substring(5)

$file = Join-Path $Dest "wastewater-nightly-$name.tgz"
Write-Host "=== Downloading backup $name" -ForegroundColor Cyan
& scp "${Server}:/tmp/ww-nightly.tgz" "$file"
$copied = $LASTEXITCODE -eq 0
& ssh $Server 'rm -f /tmp/ww-nightly.tgz' | Out-Null
if (-not $copied) { Fail 'Download failed.' }

$size = [math]::Round((Get-Item $file).Length / 1MB, 1)
Write-Host "  OK   Saved $file ($size MB)" -ForegroundColor Green
$all = Get-ChildItem $Dest -Filter 'wastewater-nightly-*.tgz' | Sort-Object Name
Write-Host "  $($all.Count) backup file(s) in $Dest. Delete old ones yourself when no longer needed."
