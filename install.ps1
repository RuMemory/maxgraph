#Requires -Version 5.1
<#
install.ps1 — MaxGraph installer for Windows (macOS/Linux: install.sh).
Run from the root of the cloned repo:
  powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 [-Yes]

Steps: working Docker? (checked WITHOUT elevation) → if missing, with the user's
consent: enable WSL2 (may reboot — continues automatically via RunOnce) →
install Rancher Desktop (free, Docker-compatible; winget or MSI from GitHub,
verified by sha512 + Authenticode signature) with a machine-wide moby/
no-kubernetes defaults profile → HOST_FP/INSTALL_FLAVOR into .env →
docker compose up -d --build → wait for healthz.

Exit codes (read by the installing AI agent, see AGENTS.md):
  0 — MaxGraph is running in Docker
  2 — reboot required/started; the installer continues automatically at logon
  3 — engine declined or impossible → Python fallback (AGENTS.md, step 3a)
  1 — unexpected error (see install.log)
#>
param([switch]$Yes, [switch]$Resume)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$StateFile = Join-Path $PSScriptRoot 'install-state.json'
$script:Flavor = ''
$script:TranscriptOn = $false
$RunOnceKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce'

function Log([string]$m) { Write-Host "[maxgraph-install] $m" }

function Write-State([string]$status) {
  $o = [ordered]@{ status = $status; flavor = $script:Flavor;
                   updated = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
  if ($status -eq 'reboot-pending') {  # integrity anchor for the RunOnce resume
    $o['script_sha256'] = (Get-FileHash -Path $PSCommandPath -Algorithm SHA256).Hash
  }
  ($o | ConvertTo-Json -Compress) | Set-Content -Path $StateFile -Encoding UTF8
}

function Stop-Log { if ($script:TranscriptOn) { try { Stop-Transcript | Out-Null } catch {} } }

function Python-Fallback {
  Write-State 'fallback-python'
  Write-Host ''
  Write-Host 'Docker engine was NOT installed (declined or failed).'
  Write-Host 'Falling back to plain Python mode — tell the user explicitly what this loses:'
  Write-Host '  - no automatic updates (manual only: git pull && pip install -U ...)'
  Write-Host '  - no autostart - the server must be started again after every reboot'
  Write-Host '  - the server listens on 0.0.0.0 (reachable from the local network; token required)'
  Write-Host 'Switch to Docker later: install Docker and re-run this script - .\data is reused.'
  Write-Host 'Python mode (from the repo root):'
  Write-Host '  pip install "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"'
  Write-Host '  $env:MEMORY_DIR="./data"; python app/app.py'
  Stop-Log
  exit 3
}

function Consent([string]$msg) {
  if ($Yes) { return $true }
  $a = Read-Host "$msg [y/N]"
  return ($a -match '^(y|yes|д|да)$')
}

function Docker-Ok {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
  & docker compose version *> $null
  if ($LASTEXITCODE -ne 0) { return $false }
  & docker info *> $null
  return ($LASTEXITCODE -eq 0)
}

function Wait-Engine([int]$minutes = 10) {
  for ($i = 0; $i -lt ($minutes * 12); $i++) {
    if (Docker-Ok) { return $true }
    Start-Sleep 5
  }
  return $false
}

function Install-Rancher {
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Log 'Installing Rancher Desktop via winget...'
    & winget install --id SUSE.RancherDesktop -e --silent `
      --accept-package-agreements --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -eq 0) { return $true }
    Log 'winget failed - trying the direct MSI...'
  }
  Log 'Resolving the latest Rancher Desktop release...'
  $rel = Invoke-RestMethod -UseBasicParsing 'https://api.github.com/repos/rancher-sandbox/rancher-desktop/releases/latest'
  $ver = $rel.tag_name.TrimStart('v')
  $msi = "Rancher.Desktop.Setup.$ver.msi"
  $base = "https://github.com/rancher-sandbox/rancher-desktop/releases/download/v$ver"
  # fresh private dir: no predictable path for a local swap between checks
  $dir = Join-Path $env:TEMP ("mg-rancher-" + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $dir -Force | Out-Null
  $tmp = Join-Path $dir $msi
  try {
    Log "Downloading $msi (verified via sha512 + Authenticode)..."
    Invoke-WebRequest -UseBasicParsing "$base/$msi" -OutFile $tmp
    $expected = (((Invoke-WebRequest -UseBasicParsing "$base/$msi.sha512sum").Content) -split '\s+')[0].ToLower()
    $actual = (Get-FileHash $tmp -Algorithm SHA512).Hash.ToLower()
    if ($actual -ne $expected) { Log "SHA512 mismatch ($actual != $expected) - aborted"; return $false }
    $sig = Get-AuthenticodeSignature $tmp
    if ($sig.Status -ne 'Valid' -or "$($sig.SignerCertificate.Subject)" -notmatch 'SUSE|Rancher') {
      Log "Authenticode check failed ($($sig.Status), $($sig.SignerCertificate.Subject)) - aborted"
      return $false
    }
    Log 'Installing (msiexec, silent)...'
    $p = Start-Process msiexec.exe -ArgumentList '/i', "`"$tmp`"", '/qn', '/norestart' -Wait -PassThru
    return ($p.ExitCode -eq 0)
  } finally {
    Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
  }
}

function Write-RancherProfile {  # skip the first-run GUI dialog: moby engine, kubernetes off.
  $d = 'HKLM:\SOFTWARE\Policies\Rancher Desktop\Defaults'   # machine-wide: correct hive even when UAC ran under other credentials
  New-Item -Path $d -Force | Out-Null
  New-ItemProperty -Path $d -Name 'version' -PropertyType DWord -Value 18 -Force | Out-Null
  New-Item -Path "$d\containerEngine" -Force | Out-Null
  New-ItemProperty -Path "$d\containerEngine" -Name 'name' -PropertyType String -Value 'moby' -Force | Out-Null
  New-Item -Path "$d\kubernetes" -Force | Out-Null
  New-ItemProperty -Path "$d\kubernetes" -Name 'enabled' -PropertyType DWord -Value 0 -Force | Out-Null
}

function Find-RancherBin {
  @("$env:LOCALAPPDATA\Programs\Rancher Desktop\resources\resources\win32\bin",
    "$env:ProgramFiles\Rancher Desktop\resources\resources\win32\bin",
    "${env:ProgramFiles(x86)}\Rancher Desktop\resources\resources\win32\bin") |
    Where-Object { $_ -and (Test-Path (Join-Path $_ 'rdctl.exe')) } |
    Select-Object -First 1
}

function Upsert-Env([string]$key, [string]$val) {
  $envFile = Join-Path $PSScriptRoot '.env'
  if (-not (Test-Path $envFile)) { Copy-Item (Join-Path $PSScriptRoot '.env.example') $envFile }
  $re = "^$([regex]::Escape($key))="
  $lines = @(Get-Content $envFile)
  $existing = @($lines | Where-Object { $_ -match $re }) | Select-Object -First 1
  if ($key -eq 'HOST_FP' -and $existing -match "$re.+") { return }  # never overwrite a non-empty HOST_FP
  $lines = @($lines | Where-Object { $_ -notmatch $re }) + "$key=$val"
  # no BOM: docker compose trips on a BOM in .env
  [System.IO.File]::WriteAllLines($envFile, $lines, (New-Object System.Text.UTF8Encoding($false)))
}

function Finish-Install {  # shared final for both engine paths: .env → compose → healthz
  Log 'Writing .env (HOST_FP, INSTALL_FLAVOR)...'
  $guid = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography' -ErrorAction SilentlyContinue).MachineGuid
  if ($guid) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $fp = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes("$guid"))).Replace('-', '').ToLower()
    Upsert-Env 'HOST_FP' $fp   # the raw MachineGuid never leaves the host - only its sha256
  } else {
    Log 'WARN: no MachineGuid - HOST_FP not set'
  }
  Upsert-Env 'INSTALL_FLAVOR' $script:Flavor

  Log 'Building and starting MaxGraph (docker compose up -d --build)...'
  & docker compose up -d --build
  if ($LASTEXITCODE -ne 0) {
    Log 'compose failed - see install.log'
    Write-State 'error'; Stop-Log; exit 1
  }
  $ok = $false
  for ($i = 0; $i -lt 60; $i++) {
    try {
      if ((Invoke-WebRequest -UseBasicParsing 'http://localhost:8787/healthz' -TimeoutSec 5).StatusCode -eq 200) { $ok = $true; break }
    } catch {}
    Start-Sleep 2
  }
  if (-not $ok) {
    Log 'healthz did not answer - check: docker compose logs maxgraph'
    Write-State 'error'; Stop-Log; exit 1
  }
  Write-State 'done'
  Log "MaxGraph is running (engine: $script:Flavor)."
  Log 'Dashboard: http://localhost:8787/app | MCP: http://localhost:8787/mcp'
  Log 'Access token: data\local_token.txt'
  Stop-Log
  exit 0
}

# --- resume integrity (before elevation): RunOnce resume only with an unchanged script ---
if ($Resume) {
  $st = $null
  if (Test-Path $StateFile) { try { $st = Get-Content $StateFile -Raw | ConvertFrom-Json } catch {} }
  $cur = (Get-FileHash -Path $PSCommandPath -Algorithm SHA256).Hash
  if (-not $st -or $st.status -ne 'reboot-pending' -or $st.script_sha256 -ne $cur) {
    Log 'Resume refused: the installer changed since the reboot (or no pending state).'
    Log 'Re-run install.ps1 manually to continue with fresh consent.'
    Remove-ItemProperty $RunOnceKey -Name 'MaxGraphInstall' -ErrorAction SilentlyContinue
    exit 1
  }
}

# --- engine check WITHOUT elevation (the Docker Desktop path needs no admin) ---
if (Docker-Ok) { $script:Flavor = 'docker' }
else {
  $dd = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
  if (Test-Path $dd) {
    Log 'Docker Desktop found but not running - starting it...'
    Start-Process $dd   # as the user, not as admin
    if (Wait-Engine 6) { $script:Flavor = 'docker' }
  }
}

if ($script:Flavor) { Finish-Install }

# --- from here on admin is required: one UAC prompt, only when an engine is truly missing ---
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
  Log 'Elevating (UAC prompt)...'
  $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
  if ($Yes) { $argList += '-Yes' }
  if ($Resume) { $argList += '-Resume' }
  try {
    $p = Start-Process powershell.exe -Verb RunAs -ArgumentList $argList -Wait -PassThru
  } catch {
    Log 'Elevation declined by the user.'
    exit 3
  }
  exit $p.ExitCode
}
Start-Transcript -Path (Join-Path $PSScriptRoot 'install.log') -Append | Out-Null
$script:TranscriptOn = $true

try {
  if (Docker-Ok) { $script:Flavor = 'docker'; Finish-Install }  # the engine came up while UAC was pending

  if (-not (Consent 'Docker not found. Install Rancher Desktop (free, Docker-compatible, ~650 MB download; admin rights; a reboot may be required - the installer continues automatically afterwards)?')) {
    Python-Fallback
  }
  if ([Environment]::OSVersion.Version.Build -lt 22000) {
    Log 'NOTE: Windows 10 detected - Rancher Desktop officially requires Windows 11. Will try; on failure we fall back to Python mode.'
  }
  # WSL2 must be enabled BEFORE the Rancher installer; enabling needs a reboot
  $wsl = Get-WindowsOptionalFeature -Online -FeatureName 'Microsoft-Windows-Subsystem-Linux'
  $vmp = Get-WindowsOptionalFeature -Online -FeatureName 'VirtualMachinePlatform'
  if ($wsl.State -ne 'Enabled' -or $vmp.State -ne 'Enabled') {
    Log 'Enabling WSL2 features...'
    Enable-WindowsOptionalFeature -Online -FeatureName 'Microsoft-Windows-Subsystem-Linux' -All -NoRestart | Out-Null
    Enable-WindowsOptionalFeature -Online -FeatureName 'VirtualMachinePlatform' -All -NoRestart | Out-Null
    Write-State 'reboot-pending'
    # HKLM RunOnce: fires at the first logon of any user (covers UAC under
    # a different admin account while the buyer is the one logging in)
    $cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Yes -Resume"
    Set-ItemProperty $RunOnceKey -Name 'MaxGraphInstall' -Value $cmd
    Log 'A REBOOT is required. The installer will continue automatically after you log back in.'
    if ($Yes -or (Consent 'Reboot now?')) {
      Log 'Rebooting in 15 seconds - save your work.'
      & shutdown.exe /r /t 15 /c 'MaxGraph: reboot to enable WSL2; the installation continues automatically after logon.' | Out-Null
    } else {
      Log 'Reboot manually when ready, then re-run install.ps1 (auto-resume cancelled).'
      Remove-ItemProperty $RunOnceKey -Name 'MaxGraphInstall' -ErrorAction SilentlyContinue
    }
    Stop-Log
    exit 2
  }
  & wsl.exe --install --no-distribution *> $null  # WSL kernel; no-op if present
  & wsl.exe --set-default-version 2 *> $null
  if (-not (Install-Rancher)) { Python-Fallback }
  $bin = Find-RancherBin
  if (-not $bin) { Log 'Rancher installed but rdctl not found'; Python-Fallback }
  Write-RancherProfile
  & (Join-Path $bin 'rdctl.exe') set --container-engine=moby --kubernetes-enabled=false *> $null  # pre-existing containerd config
  Log 'Starting Rancher Desktop (first start downloads VM images - can take minutes)...'
  & (Join-Path $bin 'rdctl.exe') start *> $null
  $env:PATH = "$bin;$env:PATH"
  if (-not (Wait-Engine 10)) {
    Log 'Engine did not come up in 10 minutes (virtualization off in BIOS? needs VT-x/AMD-V).'
    Python-Fallback
  }
  $script:Flavor = 'rancher'
  Finish-Install
} catch {
  Log "ERROR: $($_.Exception.Message)"
  Write-State 'fallback-python'
  Stop-Log
  exit 3
}
