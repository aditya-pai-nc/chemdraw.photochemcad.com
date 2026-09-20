<#
.SYNOPSIS
    Install and run ChemDraw Processor on a Windows host. Run as Administrator.

.DESCRIPTION
    One script for the whole box: Python venv, dependencies, API token, frontend
    build, autostart, firewall, then a verification pass that actually checks
    ChemDraw rather than assuming it.

    THE ONE THING THAT MATTERS

    ChemDraw is a desktop application. COM can only launch it from a session
    that has a desktop. A Windows Service runs in session 0 and has none, so
    `Documents.Open` fails with CO_E_SERVER_EXEC_FAILURE (0x80080005) and the
    whole pipeline is dead on arrival — which is what an NSSM-based install
    produces, however correct the rest of it looks.

    So the default here is a Scheduled Task that runs At Log On as you, in your
    interactive session. The machine must be logged in — on a cloud instance,
    connect once over RDP and keep the session alive; do not sign out.

    -Mode Service is kept for the interpolation and CAS halves, which need no
    ChemDraw. It refuses to pretend the ChemDraw pipeline will work.

.PARAMETER Mode
    Task    (default) Scheduled Task at logon, interactive. ChemDraw works.
    Service NSSM Windows Service, session 0. ChemDraw will NOT work.

.PARAMETER BindHost
    Address uvicorn binds. Default 127.0.0.1. Use 0.0.0.0 only when something
    off-box must reach it (a Vercel frontend), in which case an API token is
    generated automatically — see -ApiToken.

.PARAMETER ApiToken
    Shared secret the frontend must send. Generated if omitted and BindHost is
    not loopback. Written to backend\.env. Set the same value as
    CHEMDRAW_API_TOKEN on the frontend.

.PARAMETER WithFrontend
    Also npm ci, next build, and autostart `next start` on port 3000.

.PARAMETER SkipFirewall
    Do not touch Windows Firewall.

.EXAMPLE
    .\deploy-windows.ps1
    Local, loopback only, ChemDraw working.

.EXAMPLE
    .\deploy-windows.ps1 -BindHost 0.0.0.0
    Reachable from a Vercel frontend. Prints the generated API token.
#>

[CmdletBinding()]
param(
    [ValidateSet('Task', 'Service')]
    [string]$Mode = 'Task',

    [string]$BindHost = '127.0.0.1',
    [int]$Port = 8000,
    [string]$ApiToken,
    [switch]$WithFrontend,
    [switch]$SkipFirewall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot   = $PSScriptRoot
$BackendDir = Join-Path $RepoRoot 'backend'
$FrontendDir= Join-Path $RepoRoot 'frontend'
$VenvDir    = Join-Path $BackendDir 'venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$LogDir     = Join-Path $BackendDir 'logs'
$EnvFile    = Join-Path $BackendDir '.env'
$TaskName   = 'ChemDrawBackend'
$ServiceName= 'ChemDrawBackend'
$FrontTask  = 'ChemDrawFrontend'

$script:Warnings = @()

function Step   { param([string]$m) Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Ok     { param([string]$m) Write-Host "  [ok]   $m" -ForegroundColor Green }
function Info   { param([string]$m) Write-Host "  [info] $m" -ForegroundColor Gray }
function Warn   { param([string]$m) Write-Host "  [warn] $m" -ForegroundColor Yellow; $script:Warnings += $m }
function Fail   { param([string]$m) throw $m }

# ---------------------------------------------------------------------------
# 0. Preflight
# ---------------------------------------------------------------------------
Step 'Preflight'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail 'Run this in an Administrator PowerShell. It installs autostart and a firewall rule.'
}
Ok "Administrator, running as $($identity.Name)"

if (-not (Test-Path $BackendDir)) { Fail "No backend directory at $BackendDir. Run this from the repo root." }
Ok "Repository: $RepoRoot"

if ($Mode -eq 'Service') {
    Warn 'Mode=Service runs in session 0, which has no desktop. ChemDraw cannot start there,'
    Warn 'so the .cdx pipeline WILL fail with CO_E_SERVER_EXEC_FAILURE. Interpolation and CAS'
    Warn 'lookups are unaffected. Use -Mode Task if you need ChemDraw.'
}

# Loopback-only needs no secret; anything else is on a network and does.
$exposed = $BindHost -notin @('127.0.0.1', 'localhost')
if ($exposed -and -not $ApiToken) {
    $existing = $null
    if (Test-Path $EnvFile) {
        $line = Select-String -Path $EnvFile -Pattern '^\s*CHEMDRAW_API_TOKEN\s*=\s*(.+)$' |
                Select-Object -First 1
        if ($line) { $existing = $line.Matches[0].Groups[1].Value.Trim() }
    }
    if ($existing) {
        $ApiToken = $existing
        Info 'Reusing the CHEMDRAW_API_TOKEN already in backend\.env'
    } else {
        $bytes = New-Object byte[] 32
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $ApiToken = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
        Info 'Generated a new CHEMDRAW_API_TOKEN'
    }
}
if ($exposed) {
    Warn "Binding to $BindHost makes this reachable off-box. An API token is required and is set below."
}

# ---------------------------------------------------------------------------
# 1. Python environment
# ---------------------------------------------------------------------------
Step 'Python environment'

if (-not (Test-Path $VenvPython)) {
    $sysPython = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $sysPython) { Fail 'Python is not on PATH. Install Python 3.10+ and re-run.' }
    Info "Creating venv with $($sysPython.Source)"
    & $sysPython.Source -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Fail 'venv creation failed.' }
}
$pyVersion = (& $VenvPython -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
Ok "venv Python $pyVersion"

Info 'Installing dependencies (this takes a few minutes on a fresh box)'
& $VenvPython -m pip install --upgrade pip --quiet
& $VenvPython -m pip install -r (Join-Path $BackendDir 'requirements.txt') --quiet
if ($LASTEXITCODE -ne 0) { Fail 'pip install failed. See the output above.' }
Ok 'Dependencies installed'

# pywin32 registers COM support in a post-install step that pip does not run.
# Without it, win32com imports but Dispatch behaves erratically.
$pywin32Post = Join-Path $VenvDir 'Scripts\pywin32_postinstall.py'
if (Test-Path $pywin32Post) {
    & $VenvPython $pywin32Post -install -silent 2>$null | Out-Null
    Ok 'pywin32 COM registration refreshed'
}

# ---------------------------------------------------------------------------
# 2. Configuration
# ---------------------------------------------------------------------------
Step 'Configuration'

if (-not (Test-Path $EnvFile)) {
    $example = Join-Path $BackendDir '.env.example'
    if (Test-Path $example) { Copy-Item $example $EnvFile; Ok 'Created backend\.env from .env.example' }
    else { New-Item -ItemType File -Path $EnvFile | Out-Null; Ok 'Created an empty backend\.env' }
}

function Set-EnvValue {
    param([string]$Key, [string]$Value)
    $lines = @(Get-Content $EnvFile -ErrorAction SilentlyContinue)
    $set = $false
    $out = foreach ($l in $lines) {
        if ($l -match "^\s*#?\s*$([regex]::Escape($Key))\s*=") { "$Key=$Value"; $set = $true }
        else { $l }
    }
    if (-not $set) { $out = @($out) + "$Key=$Value" }
    Set-Content -Path $EnvFile -Value $out -Encoding UTF8
}

if ($ApiToken) {
    Set-EnvValue -Key 'CHEMDRAW_API_TOKEN' -Value $ApiToken
    Ok 'CHEMDRAW_API_TOKEN written to backend\.env'
}

# A service has no desktop, so the keystroke route cannot work there. The code
# detects this itself, but saying so explicitly skips the detection entirely.
if ($Mode -eq 'Service') {
    Set-EnvValue -Key 'CHEMDRAW_KEYS_FALLBACK' -Value '0'
    Info 'CHEMDRAW_KEYS_FALLBACK=0 (no desktop in a service; COM only)'
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Ok "Logs: $LogDir"

# ---------------------------------------------------------------------------
# 3. Import check — fail here, not silently at boot
# ---------------------------------------------------------------------------
Step 'Import check'

Push-Location $BackendDir
try {
    & $VenvPython -c "import app" 2>&1 | Out-String | Write-Verbose
    if ($LASTEXITCODE -ne 0) {
        & $VenvPython -c "import app"   # re-run so the traceback is visible
        Fail 'backend\app.py does not import. Fix that before installing autostart.'
    }
    Ok 'app.py imports cleanly'
} finally { Pop-Location }

# ---------------------------------------------------------------------------
# 4. Remove any previous installation (either mode) so they cannot fight
# ---------------------------------------------------------------------------
Step 'Clearing previous installation'

$existingSvc = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($existingSvc) {
    Info "Stopping and removing the '$ServiceName' service"
    try { Stop-Service $ServiceName -Force -ErrorAction SilentlyContinue } catch {}
    $nssm = 'C:\nssm\nssm.exe'
    if (Test-Path $nssm) { & $nssm remove $ServiceName confirm | Out-Null }
    else { & sc.exe delete $ServiceName | Out-Null }
    Start-Sleep -Seconds 2
    Ok 'Previous service removed'
}

foreach ($t in @($TaskName, $FrontTask)) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t -Confirm:$false
        Ok "Removed existing scheduled task '$t'"
    }
}

# Anything still holding the port would make the health check lie.
$holders = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
foreach ($h in $holders) {
    try {
        $p = Get-Process -Id $h.OwningProcess -ErrorAction SilentlyContinue
        if ($p) { Info "Stopping stale listener on port $Port (PID $($p.Id), $($p.ProcessName))"; Stop-Process -Id $p.Id -Force }
    } catch {}
}

# ---------------------------------------------------------------------------
# 5. Launcher
# ---------------------------------------------------------------------------
Step 'Launcher'

$runCmd = Join-Path $BackendDir 'run-backend.cmd'
@"
@echo off
rem Generated by deploy-windows.ps1 - edits will be overwritten.
cd /d "$BackendDir"
set PYTHONIOENCODING=utf-8
rem --workers 1 is mandatory: job state and the ChemDraw queue live in process memory.
"$VenvPython" -m uvicorn app:app --host $BindHost --port $Port --workers 1 >> "$LogDir\backend.log" 2>&1
"@ | Set-Content -Path $runCmd -Encoding ASCII
Ok "Wrote $runCmd"

# ---------------------------------------------------------------------------
# 6. Autostart
# ---------------------------------------------------------------------------
Step "Autostart ($Mode)"

if ($Mode -eq 'Task') {
    $action    = New-ScheduledTaskAction -Execute $runCmd
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description 'ChemDraw Processor backend. Interactive session so ChemDraw COM can launch.' | Out-Null
    Ok "Scheduled task '$TaskName' registered (At Log On, interactive)"

    Start-ScheduledTask -TaskName $TaskName
    Ok 'Started'
}
else {
    $NssmDir = 'C:\nssm'; $NssmExe = Join-Path $NssmDir 'nssm.exe'
    New-Item -ItemType Directory -Force -Path $NssmDir | Out-Null
    if (-not (Test-Path $NssmExe)) {
        Info 'Downloading NSSM'
        $zip = Join-Path $env:TEMP 'nssm.zip'; $ext = Join-Path $env:TEMP 'nssm-extracted'
        Remove-Item $zip, $ext -Recurse -Force -ErrorAction SilentlyContinue
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri 'https://nssm.cc/ci/nssm-2.24-101-g897c7ad.zip' -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $ext -Force
        $found = Get-ChildItem -Path $ext -Recurse -Filter 'nssm.exe' |
                 Where-Object { $_.FullName -match 'win64' } | Select-Object -First 1
        if (-not $found) { Fail 'nssm.exe not found in the download.' }
        Copy-Item $found.FullName $NssmExe -Force
    }
    Ok "NSSM: $NssmExe"

    & $NssmExe install $ServiceName $runCmd | Out-Null
    & $NssmExe set $ServiceName AppDirectory  $BackendDir | Out-Null
    & $NssmExe set $ServiceName Start         SERVICE_AUTO_START | Out-Null
    & $NssmExe set $ServiceName AppStdout     (Join-Path $LogDir 'backend.log') | Out-Null
    & $NssmExe set $ServiceName AppStderr     (Join-Path $LogDir 'backend-error.log') | Out-Null
    & $NssmExe set $ServiceName AppRotateFiles 1 | Out-Null
    & $NssmExe set $ServiceName AppRotateOnline 1 | Out-Null
    & $NssmExe set $ServiceName AppRotateBytes 10485760 | Out-Null
    & $NssmExe set $ServiceName AppExit Default Restart | Out-Null
    & $NssmExe set $ServiceName AppThrottle 5000 | Out-Null
    Ok "Service '$ServiceName' installed"

    Start-Service $ServiceName
    Ok 'Started'
}

# ---------------------------------------------------------------------------
# 7. Frontend (optional)
# ---------------------------------------------------------------------------
if ($WithFrontend) {
    Step 'Frontend'
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Warn 'npm is not on PATH; skipping the frontend.'
    } else {
        Push-Location $FrontendDir
        try {
            Info 'npm ci'
            if (Test-Path 'package-lock.json') { & npm ci --silent } else { & npm install --silent }
            if ($LASTEXITCODE -ne 0) { Fail 'npm install failed.' }

            $frontEnv = Join-Path $FrontendDir '.env.local'
            $frontLines = @("CHEMDRAW_API_URL=http://127.0.0.1:$Port")
            if ($ApiToken) { $frontLines += "CHEMDRAW_API_TOKEN=$ApiToken" }
            Set-Content -Path $frontEnv -Value $frontLines -Encoding UTF8
            Ok 'Wrote frontend\.env.local'

            Info 'next build'
            & npm run build
            if ($LASTEXITCODE -ne 0) { Fail 'next build failed.' }
            Ok 'Frontend built'
        } finally { Pop-Location }

        $frontCmd = Join-Path $FrontendDir 'run-frontend.cmd'
        @"
@echo off
rem Generated by deploy-windows.ps1 - edits will be overwritten.
cd /d "$FrontendDir"
npm run start >> "$LogDir\frontend.log" 2>&1
"@ | Set-Content -Path $frontCmd -Encoding ASCII

        $fa = New-ScheduledTaskAction -Execute $frontCmd
        $ft = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
        $fp = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Highest
        $fs = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $FrontTask -Action $fa -Trigger $ft -Principal $fp -Settings $fs `
            -Description 'ChemDraw Processor frontend (next start on :3000).' | Out-Null
        Start-ScheduledTask -TaskName $FrontTask
        Ok "Scheduled task '$FrontTask' registered and started (port 3000)"

        if (-not $SkipFirewall -and $exposed) {
            if (-not (Get-NetFirewallRule -DisplayName 'ChemDraw UI 3000' -ErrorAction SilentlyContinue)) {
                New-NetFirewallRule -DisplayName 'ChemDraw UI 3000' -Direction Inbound `
                    -Protocol TCP -LocalPort 3000 -Action Allow | Out-Null
                Ok 'Firewall opened for port 3000'
            }
        }
    }
}

# ---------------------------------------------------------------------------
# 8. Firewall
# ---------------------------------------------------------------------------
if (-not $SkipFirewall) {
    Step 'Firewall'

    $ruleName = "ChemDraw API $Port"
    $rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue

    if ($rule) {
        # Re-assert the port: an earlier run may have used a different one.
        try {
            $rule | Set-NetFirewallRule -Enabled True | Out-Null
            $rule | Get-NetFirewallPortFilter | Where-Object { $_.LocalPort -ne "$Port" } | ForEach-Object {
                $rule | Set-NetFirewallRule -LocalPort $Port | Out-Null
            }
        } catch {}
        Ok "Inbound TCP $Port already allowed (rule '$ruleName')"
    } else {
        New-NetFirewallRule -DisplayName $ruleName `
            -Description 'ChemDraw Processor API. Required when the UI is served from another host.' `
            -Direction Inbound -Protocol TCP -LocalPort $Port `
            -Action Allow -Profile Any | Out-Null
        Ok "Opened inbound TCP $Port (rule '$ruleName')"
    }

    if (-not $exposed) {
        Info "uvicorn is bound to $BindHost, so nothing off-box can reach the port regardless."
        Info 'The rule is harmless here and is ready if you later switch to -BindHost 0.0.0.0.'
    }

    # Windows Firewall is only the first of two on a cloud instance, and the
    # second one is the usual reason a correct Windows setup still times out.
    if ($exposed) {
        Info ''
        Info 'AWS Lightsail / EC2 has a SECOND firewall that Windows knows nothing about.'
        Info "Open TCP $Port there too, or the port stays closed from outside:"
        Info '  Lightsail : instance > Networking > IPv4 Firewall > Add rule'
        Info '  EC2       : the instance security group > Inbound rules'
        Info 'Restrict the source to your frontend where you can; the API token is the'
        Info 'only other thing standing in front of this service.'
    }
}
else {
    Step 'Firewall'
    Info '-SkipFirewall given; no firewall rule was created or changed.'
    if ($exposed) { Warn "Port $Port may be closed to outside traffic until you open it manually." }
}

# ---------------------------------------------------------------------------
# 9. Verify
# ---------------------------------------------------------------------------
Step 'Verification'

$base = "http://127.0.0.1:$Port"
$headers = @{}
if ($ApiToken) { $headers['x-chemdraw-token'] = $ApiToken }

$health = $null
for ($i = 1; $i -le 20; $i++) {
    try { $health = Invoke-RestMethod -Uri "$base/api/health" -TimeoutSec 5; break }
    catch { Start-Sleep -Seconds 2 }
}
if (-not $health) {
    Warn "The backend did not answer on $base after 40s. Check $LogDir\backend.log"
} else {
    # Defensive: StrictMode plus a backend predating the auth field would throw
    # on a bare property access, turning a successful deploy into a crash.
    $authOn = $false
    if ($health.PSObject.Properties.Name -contains 'auth_required') { $authOn = [bool]$health.auth_required }
    Ok "API up. auth_required=$authOn"
    if ($exposed -and -not $authOn) {
        Warn 'REACHABLE OFF-BOX WITH NO AUTHENTICATION. Set CHEMDRAW_API_TOKEN and re-run.'
    }

    try {
        $cd = Invoke-RestMethod -Uri "$base/api/chemdraw" -Headers $headers -TimeoutSec 60
        $cdOk = ($cd.PSObject.Properties.Name -contains 'available') -and $cd.available
        if ($cdOk) {
            Ok "ChemDraw reachable over COM (progid $($cd.progid))"
        } else {
            Warn "ChemDraw NOT reachable: $($cd.reason)"
            if ("$($cd.reason)" -match '0x80080005|Server execution failed|-2146959355') {
                Warn 'That error means no desktop session. If you used -Mode Service, reinstall with'
                Warn '-Mode Task and stay logged in. If you used -Mode Task, make sure you are'
                Warn 'logged in on the console/RDP and the session is not disconnected.'
            }
        }
    } catch { Warn "Could not query /api/chemdraw: $($_.Exception.Message)" }

    foreach ($ep in @('ai', 'cas')) {
        try {
            $r = Invoke-RestMethod -Uri "$base/api/$ep" -Headers $headers -TimeoutSec 20
            $enabled = if ($r.PSObject.Properties.Name -contains 'enabled') { $r.enabled } else { 'n/a' }
            Ok "/api/$ep responding (enabled=$enabled)"
        } catch { Info "/api/$ep not available: $($_.Exception.Message)" }
    }
}

# ---------------------------------------------------------------------------
# 10. Summary
# ---------------------------------------------------------------------------
Step 'Summary'

Write-Host ("  Mode        : {0}" -f $(if ($Mode -eq 'Task') { 'Scheduled Task, interactive (ChemDraw works)' } else { 'Windows Service, session 0 (ChemDraw will NOT work)' }))
Write-Host "  Backend     : http://${BindHost}:$Port"
Write-Host "  Docs        : $base/docs"
Write-Host "  Logs        : $LogDir"
if ($WithFrontend) { Write-Host '  Frontend    : http://localhost:3000' }
if ($ApiToken) {
    Write-Host ''
    Write-Host '  API TOKEN (set this as CHEMDRAW_API_TOKEN on the frontend / in Vercel):' -ForegroundColor Yellow
    Write-Host "    $ApiToken" -ForegroundColor Yellow
}

if ($script:Warnings.Count) {
    Write-Host ''
    Write-Host "  $($script:Warnings.Count) warning(s):" -ForegroundColor Yellow
    $script:Warnings | ForEach-Object { Write-Host "    - $_" -ForegroundColor Yellow }
}

Write-Host ''
Write-Host '  Manage:' -ForegroundColor Gray
if ($Mode -eq 'Task') {
    Write-Host "    Start-ScheduledTask -TaskName $TaskName"
    Write-Host "    Stop-ScheduledTask  -TaskName $TaskName"
    Write-Host "    Get-ScheduledTask   -TaskName $TaskName | Get-ScheduledTaskInfo"
} else {
    Write-Host "    Start-Service $ServiceName / Stop-Service $ServiceName / Get-Service $ServiceName"
}
Write-Host "    Get-Content '$LogDir\backend.log' -Tail 40 -Wait"
Write-Host ''
Write-Host 'DONE' -ForegroundColor Green
