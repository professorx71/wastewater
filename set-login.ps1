<#
.SYNOPSIS
  Add or change the Operator (or Admin) login on the LIVE server.

.DESCRIPTION
  1. Connects to the server and shows its dashboard revision and current logins.
  2. Runs the matching web.py command there. You type the new password in a hidden prompt;
     it is never put on the command line or saved on this PC.
  3. Shows the logins again so you can see the change.

  Easiest: double-click set-login.cmd. Or in PowerShell from this folder:
      .\set-login.cmd                         # asks which role and username
      .\set-login.cmd -Role Operator -Username operator
      .\set-login.cmd -Role Admin -Username wastewater
      .\set-login.cmd -Show                   # only show current logins
#>
[CmdletBinding()]
param(
    [ValidateSet('Operator', 'Admin')][string]$Role,
    [string]$Username,
    [switch]$Show,
    [string]$Server = 'ubuntu@51.161.52.12'
)

# Native programs (ssh) write normal messages to stderr; don't treat that as a script error.
$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath $PSScriptRoot
$WebDir = '/opt/pbf-wastewater/web'

function Say([string]$t)  { Write-Host ''; Write-Host "=== $t" -ForegroundColor Cyan }
function Ok([string]$t)   { Write-Host "  OK   $t" -ForegroundColor Green }
function Warn([string]$t) { Write-Host "  !    $t" -ForegroundColor Yellow }
function Fail([string]$t) { Write-Host ''; Write-Host "STOPPED: $t" -ForegroundColor Red; exit 1 }

# Read-only check, run on the server as the dashboard's own user. Prints no password data.
$Inspect = @'
import json, re, sys
web_dir = sys.argv[1]
sys.path.insert(0, web_dir)
src = open(web_dir + '/web.py', encoding='utf-8').read()
m = re.search(r"'revision':'([0-9.]+)'", src)
print('REVISION=' + (m.group(1) if m else 'unknown'))
print('MODE=' + ('db' if 'def set_login' in src else 'file' if 'def set_operator' in src else 'none'))
import web
if hasattr(web, 'account_summary'):
    with web.appdb() as conn:
        logins = web.account_summary(conn)
else:
    logins = {}
    for role, path in (('admin', web.AUTH), ('operator', web.OP_AUTH)):
        try:
            logins[role] = {'enabled': True, 'username': json.loads(path.read_text())['username'], 'source': 'file'}
        except Exception:
            logins[role] = {'enabled': False, 'username': None, 'source': None}
for role in ('admin', 'operator'):
    v = logins[role]
    state = 'username %s (%s)' % (v['username'], 'set on Settings page' if v['source'] == 'settings' else 'original server file') if v['enabled'] else 'NOT SET / turned off'
    print('LOGIN %-8s %s' % (role.capitalize(), state))
'@

function Inspect-Server {
    $out = $Inspect | & ssh $Server "cd $WebDir && sudo -n -u pbf-wastewater python3 - $WebDir" 2>&1
    if ($LASTEXITCODE -ne 0) {
        $out | ForEach-Object { Write-Host "  $_" }
        Fail 'Could not read the server (wrong password, no connection, or sudo needs a terminal).'
    }
    $info = @{ Revision = 'unknown'; Mode = 'none' }
    foreach ($line in $out) {
        $s = "$line"
        if ($s -like 'REVISION=*') { $info.Revision = $s.Substring(9) }
        elseif ($s -like 'MODE=*') { $info.Mode = $s.Substring(5) }
        elseif ($s -like 'LOGIN *') { Write-Host ('  ' + $s.Substring(6)) }
    }
    return $info
}

foreach ($tool in @('ssh')) { if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Fail "'$tool' is not installed." } }

Say "Live server $($Server.Split('@')[-1]): current logins"
Write-Host '  If asked, type the SERVER password (nothing shows while typing).'
$info = Inspect-Server
Ok "Dashboard revision on the server: $($info.Revision)"
if ($Show) { exit 0 }

if (-not $Role) {
    $r = Read-Host 'Which login do you want to set? Type O for Operator or A for Admin [O]'
    if ($r -match '^(a|admin)$') { $Role = 'Admin' } else { $Role = 'Operator' }
}
if (-not $Username) {
    $default = 'operator'; if ($Role -eq 'Admin') { $default = 'wastewater' }
    $Username = Read-Host "Username for the $Role login [$default]"
    if (-not $Username) { $Username = $default }
}
if ($Username -notmatch '^[A-Za-z0-9._-]{1,64}$') { Fail 'Username may only use letters, numbers, dot, dash or underscore (max 64).' }

switch ($info.Mode) {
    'db' {
        # Rev 1.2.3+: login stored in the dashboard database; run as the dashboard's user.
        $flag = '--set-operator'; if ($Role -eq 'Admin') { $flag = '--set-admin' }
        $remote = "sudo -u pbf-wastewater python3 $WebDir/web.py $flag $Username"
        $minLen = 4; if ($Role -eq 'Admin') { $minLen = 6 }
    }
    'file' {
        # Rev 1.2.2: Operator login stored in /etc/pbf-wastewater/operator-auth.json (needs root).
        if ($Role -eq 'Admin') { Fail "The server runs Rev $($info.Revision), which cannot change the Admin password by command. Deploy 1.2.3 or later first (.\release.cmd)." }
        $remote = "sudo python3 $WebDir/web.py --set-operator $Username"
        $minLen = 10
    }
    default { Fail "The server runs Rev $($info.Revision), which has no login command. Deploy 1.2.3 or later first (.\release.cmd)." }
}

Say "Set the $Role login '$Username' on the live server"
Write-Host "  You will be asked for: the SERVER password (if needed), then the NEW $Role password twice."
Write-Host "  The new password must be at least $minLen characters. Nothing shows while typing."
if ($Role -eq 'Operator') { Write-Host '  There is one Operator login: this replaces any existing Operator username/password.' }
if ((Read-Host 'Continue? (y/N)') -notmatch '^(y|yes)$') { Fail 'Cancelled. Nothing was changed.' }

& ssh -t $Server $remote
if ($LASTEXITCODE -ne 0) { Fail 'The login was NOT changed (see the message above).' }

Say 'Logins on the live server now'
$null = Inspect-Server
Ok "Done. Sign in at https://$($Server.Split('@')[-1]) as '$Username' (use Switch user or a private window)."
