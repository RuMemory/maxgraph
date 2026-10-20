#!/usr/bin/env bash

















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
TD="$(mktemp -d 2>/dev/null || mktemp -d -t mg-uninstall)"
trap 'rm -rf "$TD"' EXIT

log() { echo "[maxgraph-uninstall] $*"; }
run() {
  if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; return; fi
  "$@" || { rc=$?; FAILED=$((FAILED+1)); log "WARN: '$*' failed (exit $rc)"; }
}
consent() {
  [ "$YES" = 1 ] && return 0
  printf '%s [y/N] ' "$1"
  local a
  read -r a || return 1
  case "$a" in y|Y|yes|YES|д|Д|да|ДА) return 0 ;; *) return 1 ;; esac
}

OS="$(uname -s)"


if [ "$WIPE" = 0 ] && [ "$YES" = 0 ]; then
  consent "Delete the MaxGraph MEMORY data too (./data: knowledge graph, license, token — IRREVERSIBLE; default: KEEP)?" && WIPE=1
fi
if [ "$WIPE" = 1 ]; then
  log "Data will be WIPED (./data + updater backups)."
  log "Note: the trial stays anchored to this machine (hardware fingerprint) — wiping data does NOT restart it; a paid license is restored by email."
else
  log "Data is kept (./data + updater backups)."
fi


if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  if [ -f docker-compose.yml ]; then
    if [ "$WIPE" = 1 ]; then
      run docker compose down --rmi all -v --remove-orphans
    else
      run docker compose down --rmi all --remove-orphans
    fi
  else
    log "no docker-compose.yml here — cleanup by fixed names only"
  fi


  left="$(docker ps -a --format '{{.Names}}' | grep -E '^maxgraph(-updater)?$' || true)"

  [ -n "$left" ] && run docker rm -f $left
  if [ "$WIPE" = 1 ]; then
    vols="$(docker volume ls -q | grep -E '(^|_)updater-state$' || true)"

    [ -n "$vols" ] && run docker volume rm $vols
  fi
  if [ "$DRY" = 0 ]; then
    left="$(docker ps -a --format '{{.Names}}' | grep -E '^maxgraph(-updater)?$' || true)"
    [ -n "$left" ] && { FAILED=$((FAILED+1)); log "WARN: containers still present: $(echo $left | tr '\n' ' ')"; }
  fi
else
  log "Docker not found/running — skipping container cleanup (Python mode or engine already removed)."
fi


if [ "$WIPE" = 1 ] && [ -d data ]; then
  root="$(pwd -P)"
  target="$(cd data && pwd -P)"
  if [ "$target" = "$root/data" ]; then
    run rm -rf "$target"
  else
    FAILED=$((FAILED+1))
    log "REFUSING to remove '$target' — not the install folder's own ./data; remove it by hand"
  fi
fi




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
      run rm -f "$p"
    else
      log "keeping $p — content differs from the install-time profile (not ours)"
    fi
  fi
fi

if [ -f hooks/register_hook.py ] && command -v python3 >/dev/null 2>&1; then
  if [ "$DRY" = 1 ]; then python3 hooks/register_hook.py --auto --remove --dry-run || true
  else python3 hooks/register_hook.py --auto --remove || true; fi
fi
[ -d "$HOME/.maxgraph" ] && run rm -rf "$HOME/.maxgraph"


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
