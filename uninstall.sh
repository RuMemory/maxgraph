#!/usr/bin/env bash
# uninstall.sh — MaxGraph uninstaller for macOS and Linux (Windows: uninstall.ps1).
# Run from the root of the install folder:
#   bash uninstall.sh [--yes] [--wipe-data] [--dry-run]
#
# Removes: docker containers + images (maxgraph, maxgraph-updater), the
# updater-state volume (only with --wipe-data), the Rancher Desktop defaults
# profile (only when byte-identical to the one install.sh wrote), ~/.maxgraph
# (session-hook spool), the app shortcut (Linux .desktop / macOS
# ~/Applications/MaxGraph symlink).
# NOT removed: Docker/Rancher themselves (shared tools, other apps may use them),
# the MCP entries in client configs and the AGENTS.md rules block (done by the
# agent — AGENTS.md, "Удаление"/"Uninstall"), and this install folder itself.
#
# Memory data (./data + updater backups) is KEPT unless --wipe-data or an
# explicit "yes" at the prompt. --yes alone never wipes data.
#
# Exit codes: 0 — done; 1 — a step failed (see the WARN lines above).
set -uo pipefail
cd "$(dirname "$0")" || { echo "[maxgraph-uninstall] cannot cd to the script folder"; exit 1; }

YES=0; WIPE=0; DRY=0
for a in "$@"; do case "$a" in
  --yes|-y)    YES=1 ;;
  --wipe-data) WIPE=1 ;;
  --dry-run)   DRY=1 ;;
  *) echo "[maxgraph-uninstall] unknown flag: $a (use --yes | --wipe-data | --dry-run)"; exit 1 ;;
esac; done

FAILED=0
TD="$(mktemp -d 2>/dev/null || mktemp -d -t mg-uninstall)"   # private dir, no predictable /tmp paths
trap 'rm -rf "$TD"' EXIT

log() { echo "[maxgraph-uninstall] $*"; }
run() {
  if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; return; fi
  "$@" || { rc=$?; FAILED=$((FAILED+1)); log "WARN: '$*' failed (exit $rc)"; }
}
consent() {  # --yes answers for the user (agent already asked in chat)
  [ "$YES" = 1 ] && return 0
  printf '%s [y/N] ' "$1"
  local a
  read -r a || return 1
  case "$a" in y|Y|yes|YES|д|Д|да|ДА) return 0 ;; *) return 1 ;; esac
}

OS="$(uname -s)"

# --- data question first: it decides whether docker down takes -v -------------
if [ "$WIPE" = 0 ] && [ "$YES" = 0 ]; then
  consent "Delete the MaxGraph MEMORY data too (./data: knowledge graph, license, token — IRREVERSIBLE; default: KEEP)?" && WIPE=1
fi
if [ "$WIPE" = 1 ]; then
  log "Data will be WIPED (./data + updater backups)."
  log "Note: the trial stays anchored to this machine (hardware fingerprint) — wiping data does NOT restart it; a paid license is restored by email."
else
  log "Data is kept (./data + updater backups)."
fi

# --- docker side --------------------------------------------------------------
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  if [ -f docker-compose.yml ]; then
    if [ "$WIPE" = 1 ]; then
      run docker compose down --rmi all -v --remove-orphans   # containers + images + named volumes (updater-state)
    else
      run docker compose down --rmi all --remove-orphans      # named volumes stay: updater-state holds data backups
    fi
  else
    log "no docker-compose.yml here — cleanup by fixed names only"
  fi
  # compose project = folder name: after a folder rename `down` is a silent no-op,
  # so the fixed-name containers (and, on wipe, any updater volume) go explicitly
  left="$(docker ps -a --format '{{.Names}}' | grep -E '^maxgraph(-updater)?$' || true)"
  # shellcheck disable=SC2086 # word-splitting intended; names are docker-reported
  [ -n "$left" ] && run docker rm -f $left
  if [ "$WIPE" = 1 ]; then
    vols="$(docker volume ls -q | grep -E '(^|_)updater-state$' || true)"   # name-фильтр docker — substring: чужие volume не трогаем
    # shellcheck disable=SC2086
    [ -n "$vols" ] && run docker volume rm $vols
  fi
  if [ "$DRY" = 0 ]; then   # verify: nothing maxgraph-named may remain
    left="$(docker ps -a --format '{{.Names}}' | grep -E '^maxgraph(-updater)?$' || true)"
    [ -n "$left" ] && { FAILED=$((FAILED+1)); log "WARN: containers still present: $(echo $left | tr '\n' ' ')"; }
  fi
else
  log "Docker not found/running — skipping container cleanup (Python mode or engine already removed)."
fi

# --- memory data --------------------------------------------------------------
if [ "$WIPE" = 1 ] && [ -d data ]; then
  root="$(pwd -P)"
  target="$(cd data && pwd -P)"   # -P resolves symlinks: exact match = real ./data
  if [ "$target" = "$root/data" ]; then
    run rm -rf "$target"          # rm never traverses symlink children — links are unlinked
  else
    FAILED=$((FAILED+1))
    log "REFUSING to remove '$target' — not the install folder's own ./data; remove it by hand"
  fi
fi

# --- leftovers install.sh created ---------------------------------------------
# ярлык приложения (install.sh create_shortcuts / native.ensure_shortcuts):
# Linux — XDG .desktop; macOS — симлинк в ~/Applications (rm -f снимает ссылку, не цель)
if [ "$OS" = "Darwin" ]; then
  [ -L "$HOME/Applications/MaxGraph" ] && run rm -f "$HOME/Applications/MaxGraph"
else
  [ -e "$HOME/.local/share/applications/maxgraph.desktop" ] \
    && run rm -f "$HOME/.local/share/applications/maxgraph.desktop"
fi
if [ "$OS" = "Darwin" ]; then
  p="$HOME/Library/Preferences/io.rancherdesktop.profile.defaults.plist"
  if [ -f "$p" ]; then
    cat > "$TD/rd-defaults.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>version</key><integer>18</integer>
<key>containerEngine</key><dict><key>name</key><string>moby</string></dict>
<key>kubernetes</key><dict><key>enabled</key><false/></dict>
</dict></plist>
EOF
    if cmp -s "$TD/rd-defaults.plist" "$p"; then
      run rm -f "$p"              # exactly what install.sh wrote — ours
    else
      log "keeping $p — content differs from the install-time profile (not ours)"
    fi
  fi
fi
# хуки: снять регистрации из конфигов хостов ДО удаления ~/.maxgraph (v0.48.0)
if [ -f hooks/register_hook.py ] && command -v python3 >/dev/null 2>&1; then
  if [ "$DRY" = 1 ]; then python3 hooks/register_hook.py --auto --remove --dry-run || true
  else python3 hooks/register_hook.py --auto --remove || true; fi
fi
[ -d "$HOME/.maxgraph" ] && run rm -rf "$HOME/.maxgraph"   # session-hook spool + hooks.env; symlink → the link goes, never the target

# --- done ----------------------------------------------------------------------
if [ "$FAILED" -gt 0 ]; then
  log "FINISHED WITH $FAILED FAILURE(S) — see the WARN lines above; leftovers need manual removal."
else
  log "Server side removed."
fi
cat <<EOF
[maxgraph-uninstall] Left for the agent (AGENTS.md → Uninstall):
  • remove the "maxgraph" entry from the MCP client config(s)
  • remove the capture-hook registrations from the agent host config(s)
    (what to remove — docs/HOOKS.md, «Удаление»/«Removal» per host)
  • remove the block between <!-- maxgraph:start --> and <!-- maxgraph:end -->
    from the project AGENTS.md; for global agent rules — SHOW the user any
    maxgraph lines and remove only install-made blocks after their "yes"
  • delete this install folder: $(pwd)$([ "$WIPE" = 1 ] || printf ' (./data kept — memory preserved; move it out BEFORE deleting the folder)')
Docker engine / Rancher Desktop itself was NOT touched — other apps may use it.
If the install folder was renamed before uninstall, built images may remain: docker images --filter label=com.docker.compose.service=maxgraph
EOF
[ "$FAILED" -gt 0 ] && exit 1
exit 0
