#Requires -Version 5.1
<#
install.ps1 — MaxGraph installer for Windows (macOS/Linux: install.sh).
Run from the root of the cloned repo:
  powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 [-Yes]

Steps: working Docker? (this script never elevates itself) → if missing, with
the user's consent: download the official Docker Desktop installer from
docker.com (verified by its Authenticode signature) and run it — Docker's
installer requests admin rights itself, enables WSL2 and asks for the reboot
when one is needed; a reboot resumes this installer automatically (HKCU
RunOnce, only with an unchanged script) → HOST_FP/INSTALL_FLAVOR into .env →
pull the prebuilt GHCR image (local build as fallback) → up -d → wait for
healthz.

Exit codes (read by the installing AI agent, see AGENTS.md):
  0 — MaxGraph is running in Docker
  2 — Docker Desktop needs a reboot or has a dialog waiting: the installer
      continues automatically at next logon (or re-run install.ps1)
  3 — engine declined or impossible → Python fallback (AGENTS.md, step 3a)
  1 — unexpected error (see install.log)
#>
param([switch]$Yes, [switch]$Resume)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false   # pwsh 7.4+: упавшие нативные команды не должны бросать (мы читаем $LASTEXITCODE)
Set-Location $PSScriptRoot
$StateFile = Join-Path $PSScriptRoot 'install-state.json'
$script:Flavor = ''
$script:TranscriptOn = $false
$RunOnceKey = 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce'   # per-user: nothing in this script needs admin
$DockerDesktopExe = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"

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

function Clear-RunOnce { Remove-ItemProperty $RunOnceKey -Name 'MaxGraphInstall' -ErrorAction SilentlyContinue }

function Arm-RunOnce {  # armed BEFORE Docker's installer runs: a reboot at any point must not strand the install
  Write-State 'reboot-pending'
  $cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Yes -Resume"
  Set-ItemProperty $RunOnceKey -Name 'MaxGraphInstall' -Value $cmd
}

function Python-Fallback {
  Clear-RunOnce
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

function Install-DockerDesktop {
  $arch = 'amd64'   # PROCESSOR_ARCHITECTURE sees the PROCESS — an x86-emulated PowerShell on an ARM64 box would fetch the wrong installer
  if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { $arch = 'arm64' }
  $url = "https://desktop.docker.com/win/main/$arch/Docker%20Desktop%20Installer.exe"
  # fresh private dir: no predictable path for a local swap between checks
  $dir = Join-Path $env:TEMP ("mg-docker-" + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $dir -Force | Out-Null
  $tmp = Join-Path $dir 'DockerDesktopInstaller.exe'
  try {
    Log "Downloading the official Docker Desktop installer from docker.com ($arch, ~600 MB)..."
    try {
      Invoke-WebRequest -UseBasicParsing $url -OutFile $tmp
    } catch {
      Log "Download failed: $($_.Exception.Message)"
      return $false
    }
    $sig = Get-AuthenticodeSignature $tmp
    $cn = $null
    if ($sig.SignerCertificate) {
      $cn = $sig.SignerCertificate.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
    }
    if ($sig.Status -ne 'Valid' -or $cn -notin @('Docker Inc', 'Docker, Inc.')) {
      Log "Authenticode check failed (status=$($sig.Status), CN=$cn) - aborted"
      return $false
    }
    Arm-RunOnce
    Log 'Running the official Docker Desktop installer - it requests admin rights itself and enables WSL2. If it offers a reboot, agree: this installer continues automatically after logon.'
    $p = Start-Process $tmp -Wait -PassThru
    if ($p.ExitCode -ne 0) {
      Log "Docker Desktop installer exited with code $($p.ExitCode) - aborted"
      Clear-RunOnce
      return $false
    }
    return $true
  } finally {
    Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
  }
}

function Refresh-DockerPath {  # Docker's installer updates the machine PATH; this session keeps the old one
  $bin = "$env:ProgramFiles\Docker\Docker\resources\bin"
  if (Test-Path (Join-Path $bin 'docker.exe')) { $env:PATH = "$bin;$env:PATH" }
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
  Clear-RunOnce
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
  $mgv = ([string](Get-Content (Join-Path $PSScriptRoot 'VERSION') -ErrorAction SilentlyContinue)).Trim()
  if ($mgv -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {   # мусор в VERSION не должен отравить ref образа
    if ($mgv) { Log "WARN: VERSION is not semver ('$mgv') - MG_VERSION empty (image: latest)" }
    $mgv = ''
  }
  Upsert-Env 'MG_VERSION' $mgv   # pin of the GHCR image; empty → latest

  $running = ([string](& docker exec maxgraph cat /app/VERSION 2>$null)).Trim()
  if ($running -match '^[0-9]+\.[0-9]+\.[0-9]+$' -and $mgv) {
    try {
      if ([version]$running -gt [version]$mgv) {   # автообновлённая установка + старый клон: даунгрейдить нельзя
        Upsert-Env 'MG_VERSION' $running
        Log "Running MaxGraph $running is NEWER than this clone ($mgv) - the clone is stale (git pull for a fresh one). Leaving the running server untouched."
        Write-State 'done'; Stop-Log; exit 0
      }
    } catch {}
  }

  Log 'Starting MaxGraph (pulling the prebuilt image first; local build is the fallback)...'
  & docker compose pull *> $null
  if ($LASTEXITCODE -ne 0) { Log 'Prebuilt image unavailable - building locally (a few minutes)...' }
  & docker compose up -d
  if ($LASTEXITCODE -ne 0) {
    Log 'up failed - retrying with a full local build...'
    & docker compose up -d --build
  }
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

# --- resume integrity: RunOnce resume only with an unchanged script ---
if ($Resume) {
  $st = $null
  if (Test-Path $StateFile) { try { $st = Get-Content $StateFile -Raw | ConvertFrom-Json } catch {} }
  $cur = (Get-FileHash -Path $PSCommandPath -Algorithm SHA256).Hash
  if (-not $st -or $st.status -ne 'reboot-pending' -or $st.script_sha256 -ne $cur) {
    Log 'Resume refused: the installer changed since the reboot (or no pending state).'
    Log 'Re-run install.ps1 manually to continue with fresh consent.'
    Clear-RunOnce
    exit 1
  }
}

Start-Transcript -Path (Join-Path $PSScriptRoot 'install.log') -Append | Out-Null
$script:TranscriptOn = $true

try {
  if (Docker-Ok) {
    $script:Flavor = 'docker'
    if ($Resume) { $script:Flavor = 'docker-desktop' }   # resume of the install we armed
    Finish-Install
  }

  if (Test-Path $DockerDesktopExe) {
    Log 'Docker Desktop found but not running - starting it...'
    Refresh-DockerPath
    Start-Process $DockerDesktopExe   # as the user, no admin needed
    if (Wait-Engine 10) {
      $script:Flavor = 'docker'
      if ($Resume) { $script:Flavor = 'docker-desktop' }   # resume of the reboot we armed
      Finish-Install
    }
    Log 'Docker Desktop is installed but its engine is not up. Finish its first-run dialog (a sign-in offer can be skipped), then re-run install.ps1.'
    Clear-RunOnce
    Write-State 'error'; Stop-Log; exit 1
  }

  if ([Environment]::OSVersion.Version.Build -lt 19044) {
    Log 'NOTE: Docker Desktop requires Windows 10 21H2+ or Windows 11. Will try; on failure we fall back to Python mode.'
  }

  for ($attempt = 1; $attempt -le 2; $attempt++) {
    if ($attempt -eq 1) {
      $q = 'Docker not found. Download and run the official Docker Desktop installer from docker.com (~600 MB; Docker''s installer requests admin rights itself; one reboot may be needed - this installer continues automatically afterwards)?'
    } else {
      $q = 'Docker Desktop is the recommended engine for MaxGraph (the Python fallback loses auto-updates and autostart). Install the official Docker Desktop from docker.com?'
    }
    if (-not (Consent $q)) { continue }
    if (-not (Install-DockerDesktop)) { continue }
    Refresh-DockerPath
    if (Test-Path $DockerDesktopExe) {
      Log 'Starting Docker Desktop (first start can take minutes)...'
      Start-Process $DockerDesktopExe
    }
    if (Wait-Engine 10) { $script:Flavor = 'docker-desktop'; Finish-Install }
    # Engine not up: Docker's installer asked for a reboot (RunOnce is armed)
    # or its first-run dialog is still waiting on the user.
    Log 'If Docker Desktop asked for a reboot - reboot now: this installer continues automatically after logon. Otherwise finish the Docker Desktop dialog, then re-run install.ps1.'
    Stop-Log
    exit 2
  }
  Python-Fallback
} catch {
  Log "ERROR: $($_.Exception.Message)"
  Clear-RunOnce
  Write-State 'fallback-python'
  Stop-Log
  exit 3
}
