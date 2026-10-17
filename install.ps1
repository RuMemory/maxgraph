#Requires -Version 5.1
<#
install.ps1 — MaxGraph installer for Windows (macOS/Linux: install.sh).
Run from the root of the cloned repo:
  powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 [-Yes] [-ProjectDir <user project root>] [-RulesLang ru|en]

Steps: working Docker? (this script never elevates itself) → if missing, with
the user's consent: download the official Docker Desktop installer from
docker.com (verified by its Authenticode signature) and run it — Docker's
installer requests admin rights itself, enables WSL2 and asks for the reboot
when one is needed; a reboot resumes this installer automatically (HKCU
RunOnce, only with an unchanged script) → hostfp.json + HOST_FP/INSTALL_FLAVOR
into .env → pull the prebuilt GHCR image (local build as fallback) → up -d →
wait for healthz → print the next-steps package (token + MCP config) for the
installing AI agent.
-ProjectDir: the memory rules block is injected into <dir>\AGENTS.md between
<!-- maxgraph:start/end --> markers (idempotent).

Modes: default = full install; -HostfpOnly = only (re)compute hostfp.json next
to docker-compose.yml (heals an existing install — no rebuild; apply with
`docker compose up -d`; no rules injection, no package print).

Exit codes (read by the installing AI agent, see AGENTS.md):
  0 — MaxGraph is running in Docker (or hostfp.json written with -HostfpOnly)
  2 — Docker Desktop needs a reboot or has a dialog waiting: the installer
      continues automatically at next logon (or re-run install.ps1)
  3 — engine declined or impossible → Python fallback (AGENTS.md, step 1a)
  1 — unexpected error (see install.log)
#>
param([switch]$Yes, [switch]$Resume, [switch]$HostfpOnly, [string]$ProjectDir, [string]$RulesLang)

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

# --- memory rules into the user's project (-ProjectDir) ----------------------
# Injected BEFORE any engine work: only the repo file is needed, and the block
# must land on every exit path (including the Python fallback). Idempotent:
# the <!-- maxgraph:start/end --> markers are replaced in place on re-runs.
$script:RulesInjected = $false

function Get-RulesBlock([string]$lang) {
  $md = Join-Path $PSScriptRoot 'maxgraph-agent.md'
  if (-not (Test-Path $md)) { return $null }
  $text = [System.IO.File]::ReadAllText($md, [System.Text.Encoding]::UTF8)
  $hdr = if ($lang -eq 'ru') { '## Блок \(RU' } else { '## Блок \(EN' }
  $m = [regex]::Match($text, $hdr + '[\s\S]*?```text\s*\r?\n([\s\S]*?)\r?\n```')
  if ($m.Success) { return $m.Groups[1].Value }
  return $null
}

function Inject-Rules {
  if ([string]::IsNullOrWhiteSpace($ProjectDir)) { return }
  if ($RulesLang -and $RulesLang -notin @('ru','en')) {
    Log "WARN: -RulesLang '$RulesLang' - expected ru|en; falling back to OS locale"
    $RulesLang = ''
  }
  if (-not (Test-Path $ProjectDir -PathType Container)) { Log "WARN: -ProjectDir '$ProjectDir' does not exist - rules not injected"; return }
  $canon = (Resolve-Path $ProjectDir).Path.TrimEnd('\')
  if ($canon -ieq $PSScriptRoot.TrimEnd('\')) {
    Log 'WARN: -ProjectDir points at the cloned repo itself - rules belong in the USER project root; skipped'
    return
  }
  $lang = if ($RulesLang) { $RulesLang } elseif ((Get-Culture).TwoLetterISOLanguageName -eq 'ru') { 'ru' } else { 'en' }
  $block = Get-RulesBlock $lang
  if (-not $block) { Log 'WARN: rules block not found in maxgraph-agent.md'; return }
  $f = Join-Path $canon 'AGENTS.md'
  if ((Test-Path $f) -and (Get-Item $f -Force).LinkType) {
    Log "WARN: $f is a link - refusing to write through it"
    return
  }
  $start = '<!-- maxgraph:start -->'; $end = '<!-- maxgraph:end -->'
  $utf8 = New-Object System.Text.UTF8Encoding($false)   # no BOM; never Set-Content (PS5.1 writes ANSI)
  # Кодировка исходного AGENTS.md сохраняется: BOM → UTF-8+BOM; валидный UTF-8 →
  # UTF-8 без BOM; иначе ANSI (Encoding::Default) — жёсткое чтение UTF-8 портило
  # CP1251-файлы без BOM (PS5.1 RU-Windows). Если блок в ANSI не кодируется —
  # апгрейд до UTF-8 с BOM (без потерь), но никогда не кракозябры.
  $enc = $utf8
  $cur = ''
  if (Test-Path $f) {
    $bytes = [System.IO.File]::ReadAllBytes($f)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
      $cur = [System.Text.Encoding]::UTF8.GetString($bytes, 3, $bytes.Length - 3)
      $enc = New-Object System.Text.UTF8Encoding($true)
    } else {
      $strict = New-Object System.Text.UTF8Encoding($false, $true)
      try { $cur = $strict.GetString($bytes) }
      catch { $cur = [System.Text.Encoding]::Default.GetString($bytes); $enc = [System.Text.Encoding]::Default }
    }
  }
  # markers count ONLY on their own line (CRLF tolerated): the rules block
  # itself mentions them inline (uninstall instructions) — an unanchored match
  # would tear the file
  $startRe = '(?m)^' + [regex]::Escape($start) + '\r?$'
  $endRe = '(?m)^' + [regex]::Escape($end) + '\r?$'
  # переносы файла наследуются: CRLF-файл → и блок, и соединители в CRLF
  $eol = "`n"
  if ($cur.Contains("`r`n")) {
    $eol = "`r`n"
    $block = ($block -replace "`r`n", "`n") -replace "`n", "`r`n"
  }
  if ($cur -and ([regex]::IsMatch($cur, $startRe)) -and ([regex]::IsMatch($cur, $endRe))) {
    # паттерн поглощает и перевод строки после end-маркера: иначе хвост \n
    # остаётся от старого текста и смешивает EOL в CRLF-файле
    $pat = '(?ms)^' + [regex]::Escape($start) + '\r?\n.*?^' + [regex]::Escape($end) + '(\r?\n)?'
    $re = New-Object System.Text.RegularExpressions.Regex($pat)
    if (-not $re.IsMatch($cur)) {   # битая пара (end раньше start) — файл не трогаем
      Log "WARN: $f - broken marker pair (end before start?) - file untouched"
      return
    }
    $replacement = $start + $eol + $block + $eol + $end + $eol
    $new = $re.Replace($cur, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $replacement }, 1)
  } else {
    $prefix = if ($cur) { $eol } else { '' }
    $new = $cur + $prefix + $start + $eol + $block + $eol + $end + $eol
  }
  if ($enc -eq [System.Text.Encoding]::Default) {   # блок не должен терять символы
    $probe = $enc.GetString($enc.GetBytes($new))
    if ($probe -cne $new) { $enc = New-Object System.Text.UTF8Encoding($true) }
  }
  # атомарно: непредсказуемый временный файл + File.Replace (обрыв WriteAllText
  # усечёт файл пользователя; предсказуемое имя tmp — вектор symlink-подмены).
  # .NET Framework (PS5.1): backup-аргумент File.Replace не может быть $null.
  $tmp = Join-Path $canon (".mg-" + [guid]::NewGuid().ToString("N") + ".tmp")
  $bak = Join-Path $canon (".mg-" + [guid]::NewGuid().ToString("N") + ".bak")
  try {
    [System.IO.File]::WriteAllText($tmp, $new, $enc)
    if ($cur) {
      try { [System.IO.File]::Replace($tmp, $f, $bak) }
      catch { [System.IO.File]::Copy($tmp, $f, $true) }   # экзотика ФС — лучше копия, чем отказ
    } else {
      [System.IO.File]::Move($tmp, $f)
    }
  } finally {
    Remove-Item $tmp, $bak -Force -ErrorAction SilentlyContinue
  }
  Log "Memory rules written into $f (between markers)"
  $script:RulesInjected = $true
}

function Write-HookFiles {  # ~/.maxgraph: хук + hooks.env; регистрацию в конфигах хостов делает агент по docs/HOOKS.md
  $d = Join-Path $env:USERPROFILE '.maxgraph'
  if ((Test-Path $d) -and ((Get-Item $d -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    Log "WARN: $d is a reparse point - refusing to write hooks.env"; return
  }
  try { New-Item -ItemType Directory -Path $d -Force | Out-Null } catch { Log "WARN: cannot create $d"; return }
  $src = Join-Path $PSScriptRoot 'hooks\maxgraph_hook.py'
  if (Test-Path $src) {
    try { Copy-Item $src (Join-Path $d 'maxgraph_hook.py') -Force } catch { Log "WARN: hook copy failed: $($_.Exception.Message)"; return }
  } else { Log 'WARN: hooks\maxgraph_hook.py not found in the clone'; return }
  $envFile = Join-Path $d 'hooks.env'
  if (-not (Test-Path $envFile)) {   # пишем один раз: ручные правки не затираем; без BOM
    $token = ''
    $tf = Join-Path $PSScriptRoot 'data\local_token.txt'
    if (Test-Path $tf) { $token = ([System.IO.File]::ReadAllText($tf) -replace '\s','') }   # весь whitespace: внутренний перевод строки инъецирует лишнюю строку в hooks.env
    if ($token) {
      [System.IO.File]::WriteAllText($envFile, "MG_SERVER=http://localhost:8787`nMG_TOKEN=$token`n", (New-Object System.Text.UTF8Encoding($false)))
      try {  # ACL как chmod 600 в sh: только текущий пользователь
        $who = "$env:USERDOMAIN\$env:USERNAME"
        & icacls $envFile /inheritance:r /grant:r "${who}:F" | Out-Null
      } catch {}
    }
  }
  Log 'Hooks ready: ~/.maxgraph (agent registers them into its host - docs/HOOKS.md)'
}

function Register-Hooks {  # авто-регистрация хуков во всех НАЙДЕННЫХ хостах (молча; агент — фолбэк)
  $rh = Join-Path $PSScriptRoot 'hooks\register_hook.py'
  if (-not (Test-Path $rh)) { return }
  $py = $null
  foreach ($cand in @('python', 'py')) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
  }
  if (-not $py) { Log 'python not found - hooks not registered automatically (agent registers by docs/HOOKS.md)'; return }
  $rhArgs = @($rh, '--auto')
  if ($py -eq 'py') { $rhArgs = @('-3') + $rhArgs }
  try {
    & $py @rhArgs 2>$null | ForEach-Object { Log "hooks: $_" }
  } catch { Log "WARN: hook registration failed: $($_.Exception.Message)" }   # не роняет установку
}

function Show-NextSteps {  # final package for the installing agent (see AGENTS.md)
  $token = ''
  $tf = Join-Path $PSScriptRoot 'data\local_token.txt'
  if (Test-Path $tf) { $token = ([System.IO.File]::ReadAllText($tf)).Trim() }
  if (-not $token) { $token = '<see data\local_token.txt>' }
  $cfg = '"url":"http://localhost:8787/mcp","headers":{"Authorization":"Bearer ' + $token + '"}'
  $rulesText = if ($script:RulesInjected) { "already in the project AGENTS.md ($ProjectDir)" } else { 'NOT injected - paste the block from maxgraph-agent.md between <!-- maxgraph:start --> and <!-- maxgraph:end --> in the project AGENTS.md' }
  Write-Host ''
  Write-Host '================ MAXGRAPH - NEXT STEPS (for the agent) ================'
  Write-Host "1. Connect MCP to the user's host:"
  Write-Host '   url: http://localhost:8787/mcp'
  Write-Host "   header: Authorization: Bearer $token"
  Write-Host "   JSON config (any host): {""mcpServers"":{""maxgraph"":{""type"":""http"",$cfg}}}"
  Write-Host '   Claude Code: claude plugin marketplace add RuMemory/maxgraph; claude plugin install maxgraph@maxgraph'
  Write-Host "2. Memory rules: $rulesText"
  Write-Host '3. Capture hooks: auto-registered into every detected agent host (see "hooks:" log lines); if yours was skipped - one snippet from docs/HOOKS.md.'
  Write-Host '4. Verify memory: create_entities install-check -> search_nodes install-check.'
  Write-Host '5. Tell the user: fully restart the host and start a NEW session in the project folder.'
  Write-Host "   Dashboard: http://localhost:8787/app#$token (token is in the link - opens signed in)."
  Write-Host '======================================================================='
  $ri = $script:RulesInjected
  # машиночитаемая строка — через ConvertTo-Json (пути с \ и " ломали ручную сборку);
  # вызывать ПОСЛЕ Stop-Log: токен не должен попадать в транскрипт install.log
  $meta = [ordered]@{ mcp_url = 'http://localhost:8787/mcp'; token = $token; dashboard = "http://localhost:8787/app#$token"; rules_injected = $ri; project_dir = $ProjectDir }
  Write-Host ("MAXGRAPH_NEXT_STEPS=" + ($meta | ConvertTo-Json -Compress))
}

function Docker-Ok {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
  # PS 5.1: stderr нативной команды при редиректе + EAP=Stop → terminating error
  # (боевой прогон 2026-10-18); try/catch — логика не меняется, читаем $LASTEXITCODE
  try { & docker compose version *> $null } catch { return $false }
  if ($LASTEXITCODE -ne 0) { return $false }
  try { & docker info *> $null } catch { return $false }
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

function Write-HostfpJson {  # композитный отпечаток хоста → hostfp.json рядом с compose
  # Сырые идентификаторы не покидают машину: в файл идут только sha256-хеши
  # (salt + нормализованное значение). Алгоритм байт-в-байт тот же, что в
  # install.sh и app/hostfp.py: нормализация (lowercase/trim/схлопнуть пробелы/
  # срезать -:_), отброс плейсхолдеров, сортировка строк — ordinal.
  $Salt = 'maxgraph-hostfp/v1'
  function _norm([object]$v) {
    if ($null -eq $v) { return '' }
    $s = ([string]$v).Trim().ToLower() -replace '\s+', ' '
    ($s -replace '[-:_]', '').Trim()
  }
  function _isPlaceholder([string]$n) {
    if ($n -eq '') { return $true }
    if (@('03000200040005000006000700080009','to be filled by o.e.m.','default string',
          'none','not applicable','n/a','unknown','system serial number','serialnumber',
          'oem') -contains $n) { return $true }
    if ($n -match '^[0]+$') { return $true }    # все-нули
    if ($n -match '^[f]+$') { return $true }    # все-f (незаписанный UUID)
    if ($n -match '^[0 ]+$') { return $true }   # нули и пробелы
    return $false
  }
  function _sha256([string]$text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($text)))).Replace('-','').ToLower() }
    finally { $sha.Dispose() }
  }
  # user-mode сбор, без admin: MachineGuid, SMBIOS UUID, серийники платы/диска, MAC
  $mguid = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography' -ErrorAction SilentlyContinue).MachineGuid
  $uuid = (Get-CimInstance Win32_ComputerSystemProduct -ErrorAction SilentlyContinue).UUID
  $board = (Get-CimInstance Win32_BaseBoard -ErrorAction SilentlyContinue).SerialNumber
  $disk = (Get-CimInstance Win32_DiskDrive -ErrorAction SilentlyContinue |
           Where-Object { $_.MediaType -match 'Fixed' } | Select-Object -First 1).SerialNumber
  if (-not $disk) { $disk = (Get-CimInstance Win32_DiskDrive -ErrorAction SilentlyContinue | Select-Object -First 1).SerialNumber }
  $mac = (Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
          Where-Object { $_.Status -eq 'Up' } | Select-Object -First 1).MacAddress
  if (-not $mac) { $mac = (Get-CimInstance Win32_NetworkAdapter -Filter 'PhysicalAdapter=True' -ErrorAction SilentlyContinue |
                   Where-Object { $_.MACAddress } | Select-Object -First 1).MACAddress }
  $raw = [ordered]@{ os_install_id = $mguid; smbios_uuid = $uuid; board_serial = $board
                     disk_serial = $disk; mac = $mac }
  $comps = [ordered]@{}
  $lines = [System.Collections.Generic.List[string]]::new()
  foreach ($tag in $raw.Keys) {
    $n = _norm $raw[$tag]
    if (_isPlaceholder $n) { continue }
    $comps[$tag] = _sha256 "$Salt|$tag=$n"
    $lines.Add("$tag=$n")
  }
  if ($lines.Count -eq 0) { Log 'WARN: no usable hardware ids — hostfp.json not written'; return $false }
  $arr = $lines.ToArray()
  [Array]::Sort($arr, [StringComparer]::Ordinal)
  $hostFp = _sha256 ($Salt + "`n" + ($arr -join "`n"))
  $path = Join-Path $PSScriptRoot 'hostfp.json'
  if (Test-Path $path -PathType Container) { Remove-Item $path -Force }  # каталог-заглушка от docker bind-монта
  $out = [ordered]@{ v = 1; host_fp = $hostFp; components = $comps; os = 'windows'
                     created = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
  # no BOM: docker compose и python-читатель трипаются о BOM
  [System.IO.File]::WriteAllText($path, ($out | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
  Log 'hostfp.json written (machine fingerprint, hashes only)'
  return $true
}

function Finish-Install {  # shared final for both engine paths: .env → compose → healthz
  Clear-RunOnce
  Log 'Writing .env (HOST_FP, INSTALL_FLAVOR) + hostfp.json...'
  $guid = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography' -ErrorAction SilentlyContinue).MachineGuid
  if ($guid) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $fp = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes("$guid"))).Replace('-', '').ToLower()
    Upsert-Env 'HOST_FP' $fp   # the raw MachineGuid never leaves the host - only its sha256
  } else {
    Log 'WARN: no MachineGuid - HOST_FP not set'
  }
  Upsert-Env 'INSTALL_FLAVOR' $script:Flavor
  try { Write-HostfpJson | Out-Null } catch { Log "WARN: hostfp.json failed: $($_.Exception.Message)" }  # не критично: без него установка unanchored
  $mgv = ([string](Get-Content (Join-Path $PSScriptRoot 'VERSION') -ErrorAction SilentlyContinue)).Trim()
  if ($mgv -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {   # мусор в VERSION не должен отравить ref образа
    if ($mgv) { Log "WARN: VERSION is not semver ('$mgv') - MG_VERSION empty (image: latest)" }
    $mgv = ''
  }
  Upsert-Env 'MG_VERSION' $mgv   # pin of the GHCR image; empty → latest

  $running = ''
  try { $running = ([string](& docker exec maxgraph cat /app/VERSION 2>$null)).Trim() } catch { $running = '' }   # PS 5.1: native stderr + EAP=Stop → terminating; нам достаточно пустой строки
  if ($running -match '^[0-9]+\.[0-9]+\.[0-9]+$' -and $mgv) {
    try {
      if ([version]$running -gt [version]$mgv) {   # автообновлённая установка + старый клон: даунгрейдить нельзя
        Upsert-Env 'MG_VERSION' $running
        Log "Running MaxGraph $running is NEWER than this clone ($mgv) - the clone is stale (git pull for a fresh one). Leaving the running server untouched."
        Write-HookFiles
        Register-Hooks
        Write-State 'done'; Stop-Log; Show-NextSteps; exit 0
      }
    } catch {}
  }

  Log 'Starting MaxGraph (pulling the prebuilt image first; local build is the fallback)...'
  try { & docker compose pull *> $null } catch { $global:LASTEXITCODE = 1 }
  if ($LASTEXITCODE -ne 0) { Log 'Prebuilt image unavailable - building locally (a few minutes)...' }
  try { & docker compose up -d } catch { $global:LASTEXITCODE = 1 }
  if ($LASTEXITCODE -ne 0) {
    Log 'up failed - retrying with a full local build...'
    try { & docker compose up -d --build } catch { $global:LASTEXITCODE = 1 }
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
  Write-HookFiles   # хуки не критичны для запуска; агент зарегистрирует по docs/HOOKS.md
  Register-Hooks    # авто-регистрация в найденных хостах (не роняет установку)
  Write-State 'done'
  Log "MaxGraph is running (engine: $script:Flavor)."
  Stop-Log
  Show-NextSteps   # после Stop-Log: токен пакета не попадает в install.log
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

if ($HostfpOnly) {
  # лечение существующей установки без пересборки: досчитать hostfp.json рядом
  # с compose-файлом; дальше — `docker compose up -d`, чтобы контейнер подхватил
  $okHfp = $false
  try { $okHfp = Write-HostfpJson } catch { Log "ERROR: $($_.Exception.Message)" }
  Stop-Log
  if ($okHfp) { exit 0 } else { exit 1 }
}

try { Inject-Rules } catch { Log "WARN: rules injection failed: $($_.Exception.Message)" }

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
