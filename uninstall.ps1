#Requires -Version 5.1
<#
uninstall.ps1 — MaxGraph uninstaller for Windows (macOS/Linux: uninstall.sh).
Run from the root of the install folder:
  powershell -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1 [-Yes] [-WipeData] [-DryRun]

Removes: docker containers + images (maxgraph, maxgraph-updater), the
updater-state volume (only with -WipeData), the RunOnce resume key (HKCU;
legacy installs wrote it to HKLM), the Rancher Desktop defaults profile
values legacy install.ps1 wrote, ~/.maxgraph (session-hook spool).
NOT removed: Docker/Rancher themselves (shared tools, other apps may use them),
the MCP entries in client configs and the AGENTS.md rules block (done by the
agent — AGENTS.md, "Uninstall"), and this install folder itself.

Memory data (.\data + updater backups) is KEPT unless -WipeData or an explicit
"yes" at the prompt. -Yes alone never wipes data.

Exit codes: 0 — done; 1 — a step failed (see the WARN lines above).
#>
param([switch]$Yes, [switch]$WipeData, [switch]$DryRun)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$script:Failed = 0

function Log([string]$m) { Write-Host "[maxgraph-uninstall] $m" }
function Run([scriptblock]$sb, [string]$what) {
  if ($DryRun) { Write-Host "[dry-run] $what"; return }
  try { & $sb }
  catch { $script:Failed++; Log "WARN: $what failed — $($_.Exception.Message)" }
}
function Run-Docker([string[]]$dockerArgs) {
  if ($DryRun) { Write-Host "[dry-run] docker $($dockerArgs -join ' ')"; return }
  & docker @dockerArgs
  if ($LASTEXITCODE -ne 0) { $script:Failed++; Log "WARN: 'docker $($dockerArgs -join ' ')' exited with $LASTEXITCODE" }
}
function Consent([string]$msg) {
  if ($Yes) { return $true }
  $a = Read-Host "$msg [y/N]"
  return ($a -match '^(y|yes|д|да)$')
}
function Remove-Registry([scriptblock]$sb, [string]$what) {  # HKLM needs admin — warn, don't fail the run
  if ($DryRun) { Write-Host "[dry-run] $what"; return }
  try { & $sb }
  catch { $script:Failed++; Log "WARN: could not remove $what — $($_.Exception.Message)"; Log '      re-run as administrator or remove it by hand' }
}
function Remove-RegistryValue([string]$path, [string]$name, [string]$expected) {  # only what install.ps1 wrote
  if (-not (Test-Path -LiteralPath $path)) { return }
  $cur = Get-ItemProperty -LiteralPath $path -Name $name -ErrorAction SilentlyContinue
  if ($null -eq $cur) { return }
  if ("$($cur.$name)" -ne $expected) { Log "keep $path\$name — value differs from the install-time one (not ours)"; return }
  Remove-Registry { Remove-ItemProperty -LiteralPath $path -Name $name -ErrorAction Stop } "$path\$name"
}
function Remove-RegistryKeyIfEmpty([string]$path) {
  if (-not (Test-Path -LiteralPath $path)) { return }
  $k = Get-Item -LiteralPath $path
  if ($k.SubKeyCount -eq 0 -and $k.ValueCount -eq 0) {
    Remove-Registry { Remove-Item -LiteralPath $path -Force -ErrorAction Stop } "empty key $path"
  }
}
function Remove-Tree([string]$literalPath) {
  # PS 5.1 Remove-Item -Recurse TRAVERSES junctions/symlinks (fixed only in PS6):
  # never recurse a reparse point — delete the link itself instead.
  $i = Get-Item -LiteralPath $literalPath -Force
  if ($i.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    Run { $i.Delete() } "delete link $literalPath"
    return
  }
  $junctions = @(Get-ChildItem -LiteralPath $literalPath -Recurse -Force -Attributes ReparsePoint -ErrorAction SilentlyContinue)
  if ($junctions.Count -gt 0) {
    $script:Failed++
    Log "REFUSING to remove '$literalPath' — it contains junction/symlink(s) (e.g. $($junctions[0].FullName)); remove it by hand"
    return
  }
  Run { Remove-Item -LiteralPath $literalPath -Recurse -Force } "Remove-Item -LiteralPath $literalPath -Recurse -Force"
}

# --- data question first: it decides whether compose down takes -v ------------
if (-not $WipeData -and -not $Yes) {
  if (Consent 'Delete the MaxGraph MEMORY data too (.\data: knowledge graph, license, token — IRREVERSIBLE; default: KEEP)?') { $WipeData = [switch]::Present }
}
if ($WipeData) { Log 'Data will be WIPED (.\data + updater backups).'
                 Log 'Note: the trial stays anchored to this machine (hardware fingerprint) — wiping data does NOT restart it; a paid license is restored by email.' }
else { Log 'Data is kept (.\data + updater backups).' }

# --- docker side ---------------------------------------------------------------
$dockerOk = $false
if (Get-Command docker -ErrorAction SilentlyContinue) { & docker info *> $null; $dockerOk = ($LASTEXITCODE -eq 0) }
if ($dockerOk) {
  if (Test-Path -LiteralPath 'docker-compose.yml') {
    $composeArgs = @('compose', 'down', '--rmi', 'all', '--remove-orphans')
    if ($WipeData) { $composeArgs += '-v' }   # -v also drops named volumes (updater-state backups)
    Run-Docker $composeArgs
  } else {
    Log 'no docker-compose.yml here — cleanup by fixed names only'
  }
  # compose project = folder name: after a folder rename `down` is a silent no-op,
  # so the fixed-name containers (and, on wipe, any updater volume) go explicitly
  $left = @(& docker ps -a --format '{{.Names}}' | Where-Object { $_ -match '^maxgraph(-updater)?$' })
  if ($left.Count -gt 0) { Run-Docker (@('rm', '-f') + $left) }
  if ($WipeData) {
    $vols = @(& docker volume ls -q | Where-Object { $_ -match '(^|_)updater-state$' })
    foreach ($v in $vols) { Run-Docker @('volume', 'rm', $v) }
  }
  if (-not $DryRun) {   # verify: nothing maxgraph-named may remain
    $left = @(& docker ps -a --format '{{.Names}}' | Where-Object { $_ -match '^maxgraph(-updater)?$' })
    if ($left.Count -gt 0) { $script:Failed++; Log "WARN: containers still present: $($left -join ', ')" }
  }
} else {
  Log 'Docker not found/running — skipping container cleanup (Python mode or engine already removed).'
}

# --- memory data ----------------------------------------------------------------
if ($WipeData -and (Test-Path -LiteralPath 'data')) {
  $root = (Resolve-Path '.').Path
  $target = (Resolve-Path 'data').Path
  if ($target.Equals((Join-Path $root 'data'), [StringComparison]::OrdinalIgnoreCase)) {
    Remove-Tree $target
  } else {
    $script:Failed++
    Log "REFUSING to remove '$target' — not the install folder's own .\data; remove it by hand"
  }
}

# --- leftovers install.ps1 created ----------------------------------------------
$rd = 'HKLM:\SOFTWARE\Policies\Rancher Desktop\Defaults'
Remove-RegistryValue "$rd\containerEngine" 'name' 'moby'
Remove-RegistryValue "$rd\kubernetes" 'enabled' '0'
Remove-RegistryValue $rd 'version' '18'
Remove-RegistryKeyIfEmpty "$rd\containerEngine"
Remove-RegistryKeyIfEmpty "$rd\kubernetes"
Remove-RegistryKeyIfEmpty $rd
if (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'MaxGraphInstall' -ErrorAction SilentlyContinue) {
  Remove-Registry { Remove-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'MaxGraphInstall' -ErrorAction Stop } 'HKLM RunOnce MaxGraphInstall (legacy)'
}
if (Get-ItemProperty 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'MaxGraphInstall' -ErrorAction SilentlyContinue) {
  Remove-Registry { Remove-ItemProperty 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'MaxGraphInstall' -ErrorAction Stop } 'HKCU RunOnce MaxGraphInstall'
}
if (Test-Path -LiteralPath "$HOME\.maxgraph") { Remove-Tree "$HOME\.maxgraph" }   # session-hook spool

# --- done -------------------------------------------------------------------------
if ($script:Failed -gt 0) {
  Log "FINISHED WITH $script:Failed FAILURE(S) — see the WARN lines above; leftovers need manual removal."
} else {
  Log 'Server side removed.'
}
Log 'Left for the agent (AGENTS.md → Uninstall):'
Log '  • remove the "maxgraph" entry from the MCP client config(s)'
Log '  • remove the capture-hook registrations from the agent host config(s)'
Log '    (what to remove — docs/HOOKS.md, Removal section per host)'
Log '  • remove the block between <!-- maxgraph:start --> and <!-- maxgraph:end -->'
Log '    from the project AGENTS.md; for global agent rules — SHOW the user any'
Log '    maxgraph lines and remove only install-made blocks after their "yes"'
if ($WipeData) { Log "  • delete this install folder: $PSScriptRoot" }
else { Log "  • delete this install folder: $PSScriptRoot (.\data kept — memory preserved; move it out BEFORE deleting the folder)" }
Log 'Docker engine / Rancher Desktop itself was NOT touched — other apps may use it.'
Log 'If the install folder was renamed before uninstall, built images may remain: docker images --filter label=com.docker.compose.service=maxgraph'
if ($script:Failed -gt 0) { exit 1 }
exit 0
