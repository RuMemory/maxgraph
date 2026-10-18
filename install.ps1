#Requires -Version 5.1
<#
install-native.ps1 — MaxGraph NATIVE installer for Windows (no Docker).
The buyer's path: download the portable exe from the store, verify it, run it.
Run from the root of the cloned repo:
  powershell -NoProfile -ExecutionPolicy Bypass -File install-native.ps1 [-Yes] [-ProjectDir <user project root>] [-RulesLang ru|en]

Steps: store manifest -> pick windows-amd64 -> download exe + .sha256 sidecar
into %LOCALAPPDATA%\MaxGraph (atomic: .part -> verify -> rename) -> verify
sha256 from the manifest (Get-FileHash) -> stop an old maxgraph process on
reinstall -> start the exe hidden -> wait for http://localhost:8787/healthz
(up to 30s) -> read the token from data\local_token.txt -> inject the memory
rules into <ProjectDir>\AGENTS.md (idempotent, <!-- maxgraph:start/end -->) ->
write/register capture hooks -> print the next-steps package
(MAXGRAPH_NEXT_STEPS={...}).

Trust chain (owner's decision, no code-signing certificates):
  install time  = HTTPS + sha256 from the store manifest.
  ed25519 is NOT verified here: Windows PowerShell has no ed25519; the exe
  verifies the ed25519 signature itself on every auto-update.
Invoke-WebRequest does NOT set Mark-of-the-Web -> SmartScreen stays silent.
NEVER add a Zone.Identifier stream (no Set-Content -Stream Zone.Identifier).

Extra params: -StoreUrl <url> (default https://rezon-ai.ru; tests point it at
a mock), -NoStart (everything except launching the exe — for tests),
-InstallDir <dir> (default %LOCALAPPDATA%\MaxGraph).

Exit codes: 0 — installed and running (or -NoStart completed); 1 — error
(see install-native.log). Re-run = update to the latest version (idempotent).
#>
param([switch]$Yes, [string]$ProjectDir, [string]$RulesLang,
      [string]$StoreUrl = 'https://rezon-ai.ru', [switch]$NoStart, [string]$InstallDir)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false   # pwsh 7.4+: упавшие нативные команды не должны бросать
$ProgressPreference = 'SilentlyContinue'   # иначе PS5.1 рисует прогресс-бар и качает в разы медленнее
Set-Location $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($InstallDir)) { $InstallDir = Join-Path $env:LOCALAPPDATA 'MaxGraph' }
$StoreUrl = $StoreUrl.TrimEnd('/')
$script:TokenFile = Join-Path $InstallDir 'data\local_token.txt'   # exe кладёт ./data рядом с собой
$script:RulesInjected = $false
$script:TranscriptOn = $false

function Log([string]$m) { Write-Host "[maxgraph-install] $m" }
function Stop-Log { if ($script:TranscriptOn) { try { Stop-Transcript | Out-Null } catch {} } }
function Fail([string]$m) { Log "ERROR: $m"; Stop-Log; exit 1 }

# --- memory rules into the user's project (-ProjectDir) ----------------------
# Скопировано из install.ps1 без изменений: маркеры <!-- maxgraph:start/end -->,
# идемпотентно, кодировка исходного AGENTS.md сохраняется, запись атомарная.

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

# --- hooks (как в install.ps1, но токен читается из $InstallDir\data) ---------

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
    if (Test-Path $script:TokenFile) { $token = ([System.IO.File]::ReadAllText($script:TokenFile) -replace '\s','') }   # весь whitespace: внутренний перевод строки инъецирует лишнюю строку в hooks.env
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
  if (Test-Path $script:TokenFile) { $token = ([System.IO.File]::ReadAllText($script:TokenFile)).Trim() }
  if (-not $token) { $token = '<see data\local_token.txt>' }
  $cfg = '"url":"http://localhost:8787/mcp","headers":{"Authorization":"Bearer ' + $token + '"}'
  $rulesText = if ($script:RulesInjected) { "already in the project AGENTS.md ($ProjectDir)" } else { 'NOT injected - paste the block from maxgraph-agent.md between <!-- maxgraph:start --> and <!-- maxgraph:end --> in the project AGENTS.md' }
  Write-Host ''
  Write-Host '================ MAXGRAPH - NEXT STEPS (for the agent) ================'
  Write-Host "1. Connect MCP to the user's host:"
  Write-Host '   DSH: already done - row mcp-maxgraph in ~/.dsh/profiles/*/cordis.patch.yml (see "hooks:" log lines)'
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
  # вызывать ПОСЛЕ Stop-Log: токен не должен попадать в транскрипт install-native.log
  $meta = [ordered]@{ mcp_url = 'http://localhost:8787/mcp'; token = $token; dashboard = "http://localhost:8787/app#$token"; rules_injected = $ri; project_dir = $ProjectDir }
  Write-Host ("MAXGRAPH_NEXT_STEPS=" + ($meta | ConvertTo-Json -Compress))
}

# --- native download ----------------------------------------------------------

function Get-Manifest {
  $url = "$StoreUrl/api/releases/native/manifest"
  try { return Invoke-RestMethod -Uri $url -TimeoutSec 30 }
  catch {
    $resp = $_.Exception.Response
    if ($resp -and [int]$resp.StatusCode -eq 404) { Fail "Store manifest not found (404): $url" }
    Fail "Cannot fetch the store manifest ($url): $($_.Exception.Message)"
  }
}

function Select-Artifact($manifest, [string]$osName, [string]$archName) {
  if (-not $manifest.version -or $manifest.version -notmatch '^\d+\.\d+\.\d+$') {
    Fail "Store manifest has no valid semver 'version' ($(($manifest.version)))"
  }
  $arts = @($manifest.artifacts)
  if ($arts.Count -eq 0) { Fail 'Store manifest has an empty artifacts list' }
  foreach ($a in $arts) {
    if ($a.os -eq $osName -and $a.arch -eq $archName) {
      if ($a.name -notmatch '^[\w.\-]+$') { Fail "Artifact name is unsafe: '$($a.name)'" }   # path-traversal guard
      if ($a.sha256 -notmatch '^[0-9a-fA-F]{64}$') { Fail "Artifact $($a.name): sha256 is missing or malformed" }
      return $a
    }
  }
  Fail "No $osName-$archName artifact in the store manifest (version $($manifest.version))"
}

function Download-Verified($artifact) {
  # Атомарно: .part -> проверка sha256 из манифеста -> rename. Обрыв закачки
  # оставляет .part, а не «полурабочий» exe; rename после верификации.
  $exe = Join-Path $InstallDir $artifact.name
  $part = "$exe.part"
  try { New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null } catch { Fail "Cannot create ${InstallDir}: $($_.Exception.Message)" }
  $url = "$StoreUrl/api/releases/native/$($artifact.name)"
  Log "Downloading $($artifact.name) (version $($script:ManifestVersion), $($artifact.bytes) bytes)..."
  try {
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $part -TimeoutSec 600
  } catch {
    Remove-Item $part -Force -ErrorAction SilentlyContinue
    $resp = $_.Exception.Response
    if ($resp -and [int]$resp.StatusCode -eq 404) { Fail "Artifact not found on the store (404): $url" }
    Fail "Download failed ($url): $($_.Exception.Message)"
  }
  # ed25519-подпись (.sig) здесь НЕ проверяем: в Windows PowerShell нет ed25519
  # (Windows без OpenSSL из коробки — владелец осведомлён, решение security
  # review 2026-10-18, M-1; sh-установщик ed25519 проверяет — install-native.sh).
  # Цепочка доверия: HTTPS + sha256 манифеста на установке; ed25519 проверяет
  # сам exe на каждом автообновлении.
  $actual = (Get-FileHash -Path $part -Algorithm SHA256).Hash.ToLower()
  if ($actual -ne $artifact.sha256.ToLower()) {
    Remove-Item $part -Force -ErrorAction SilentlyContinue
    Fail "sha256 mismatch for $($artifact.name): expected $($artifact.sha256), got $actual"
  }
  Log 'sha256 verified against the store manifest.'
  # sidecar .sha256 (формат sha256sum) — для ручных проверок пользователем; best effort
  try { Invoke-WebRequest -UseBasicParsing -Uri "$url.sha256" -OutFile "$exe.sha256" -TimeoutSec 60 } catch {}
  # Переустановка: старый maxgraph-процесс держит exe открытым — гасим его до rename
  $old = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe })
  foreach ($p in $old) {
    Log "Stopping the running MaxGraph (pid $($p.Id)) for the update..."
    try { Stop-Process -Id $p.Id -Force -ErrorAction Stop } catch { Log "WARN: cannot stop pid $($p.Id): $($_.Exception.Message)" }
  }
  if ($old.Count -gt 0) { Start-Sleep -Seconds 2 }
  try { [System.IO.File]::Copy($part, $exe, $true) } catch { Remove-Item $part -Force -ErrorAction SilentlyContinue; Fail "Cannot install to ${exe}: $($_.Exception.Message)" }
  Remove-Item $part -Force -ErrorAction SilentlyContinue
  Log "Installed to $exe"
  return $exe
}

function Wait-Healthz([int]$seconds = 30) {
  for ($i = 0; $i -lt [math]::Ceiling($seconds / 2); $i++) {
    try {
      $r = Invoke-WebRequest -UseBasicParsing 'http://localhost:8787/healthz' -TimeoutSec 5
      # строгое равенство тела «ok», не подстрока/только 200 (MINOR-5)
      if ($r.StatusCode -eq 200 -and ([string]$r.Content).Trim() -eq 'ok') { return $true }
    } catch {}
    Start-Sleep 2
  }
  return $false
}

# --- main ---------------------------------------------------------------------

Start-Transcript -Path (Join-Path $PSScriptRoot 'install-native.log') -Append | Out-Null
$script:TranscriptOn = $true

try {
  Log "Store: $StoreUrl; install dir: $InstallDir"
  $manifest = Get-Manifest
  $script:ManifestVersion = $manifest.version
  $artifact = Select-Artifact $manifest 'windows' 'amd64'
  $exe = Download-Verified $artifact

  if ($NoStart) {
    Log 'NoStart: skipping exe launch, healthz wait and token read (test mode).'
  } else {
    Log 'Starting MaxGraph (hidden window)...'
    Start-Process -FilePath $exe -WorkingDirectory $InstallDir -WindowStyle Hidden
    if (-not (Wait-Healthz 30)) {
      Log 'healthz did not answer within 30s - check the exe output in the install dir.'
      Stop-Log
      exit 1
    }
    Log 'MaxGraph is up (http://localhost:8787).'
  }

  try { Inject-Rules } catch { Log "WARN: rules injection failed: $($_.Exception.Message)" }
  Write-HookFiles   # хуки не критичны для запуска; агент зарегистрирует по docs/HOOKS.md
  Register-Hooks    # авто-регистрация в найденных хостах (не роняет установку)
  Log "MaxGraph native installed (version $script:ManifestVersion)."
  Stop-Log
  Show-NextSteps   # после Stop-Log: токен пакета не попадает в install-native.log
  exit 0
} catch {
  Fail $_.Exception.Message
}
