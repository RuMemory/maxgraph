#!/usr/bin/env bash
# install.sh — MaxGraph installer for macOS and Linux (Windows: install.ps1).
# Run from the root of the cloned repo:  bash install.sh [--yes]
#
# Steps: working Docker? → if missing, with the user's consent install an engine
# (macOS → Rancher Desktop, free & Docker-compatible; Linux → official Docker
# Engine script) → write HOST_FP/INSTALL_FLAVOR into .env →
# docker compose up -d --build → wait for healthz.
#
# Exit codes (read by the installing AI agent, see AGENTS.md):
#   0 — MaxGraph is running in Docker
#   3 — engine declined or impossible → Python fallback (AGENTS.md, step 3)
#   1 — unexpected error (see install.log)
set -uo pipefail
umask 077   # .env and downloads must not be world-readable
cd "$(dirname "$0")"

TD="$(mktemp -d 2>/dev/null || mktemp -d -t mg-install)"   # private download dir, no predictable /tmp paths
trap 'rm -rf "$TD"' EXIT

LOG=install.log
STATE=install-state.json
YES=0
case "${1:-}" in --yes|-y) YES=1 ;; esac

OS="$(uname -s)"
case "$OS" in Darwin|Linux) ;; *)
  echo "[maxgraph-install] Unsupported OS: $OS (on Windows use install.ps1)"; exit 1 ;;
esac

DOCKER="docker"   # becomes "sudo docker" on Linux until the group applies
FLAVOR=""

log() { echo "[maxgraph-install] $*" | tee -a "$LOG"; }

write_state() {  # status flavor
  printf '{"status":"%s","flavor":"%s","updated":"%s"}\n' \
    "$1" "$2" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STATE"
}

docker_ok() { $DOCKER info >/dev/null 2>&1 && $DOCKER compose version >/dev/null 2>&1; }

wait_engine() {  # first Rancher start downloads VM images — allow 10 minutes
  local i
  for i in $(seq 1 120); do
    docker_ok && return 0
    sleep 5
  done
  return 1
}

consent() {  # $1 = question; --yes answers for the user (agent already asked in chat)
  [ "$YES" = 1 ] && return 0
  printf '%s [y/N] ' "$1"
  local a
  read -r a || return 1
  case "$a" in y|Y|yes|YES|д|Д|да|ДА) return 0 ;; *) return 1 ;; esac
}

python_fallback() {
  write_state fallback-python "$FLAVOR"
  cat <<'EOF' | tee -a "$LOG"

Docker engine was NOT installed (declined or failed).
Falling back to plain Python mode — tell the user explicitly what this loses:
  • no automatic updates (manual only: git pull && pip install -U …)
  • no autostart — the server must be started again after every reboot
  • the server listens on 0.0.0.0 (reachable from the local network; token required)
Switch to Docker later: install Docker and re-run this script — ./data is reused.
Python mode (from the repo root):
  pip install "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"
  MEMORY_DIR=./data python app/app.py
EOF
  exit 3
}

upsert_env() {  # key value — insert or replace; a non-empty HOST_FP is never overwritten
  local key="$1" val="$2"
  [ -f .env ] || cp .env.example .env
  if grep -q "^${key}=" .env; then
    if [ "$key" = "HOST_FP" ] && grep -qE '^HOST_FP=.+$' .env; then return 0; fi
    grep -v "^${key}=" .env > .env.tmp; mv .env.tmp .env   # ";" не "&&": grep -v отдаёт 1 на пустом выводе
  fi
  printf '%s=%s\n' "$key" "$val" >> .env
}

compute_host_fp() {  # sha256 of the stable hardware id; raw id never leaves the machine
  case "$OS" in
    Darwin) printf %s "$(ioreg -rd1 -c IOPlatformExpertDevice | awk -F'"' '/IOPlatformUUID/{print $4}')" | shasum -a 256 | awk '{print $1}' ;;
    Linux)  { cat /etc/machine-id 2>/dev/null || cat /var/lib/dbus/machine-id; } | grep -v '^uninitialized$' | tr -d '[:space:]' | sha256sum | awk '{print $1}' ;;
  esac
}

install_engine_macos() {
  if command -v brew >/dev/null 2>&1; then
    log "Installing Rancher Desktop via Homebrew…"
    brew install --cask rancher || return 1
  else
    log "Homebrew not found — downloading Rancher Desktop from GitHub releases…"
    local api ver arch dmg mnt
    api="$(curl -fsSL https://api.github.com/repos/rancher-sandbox/rancher-desktop/releases/latest)" || return 1
    ver="$(printf '%s' "$api" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\([^"]*\)".*/\1/p' | head -1)"
    [ -n "$ver" ] || { log "Could not resolve the latest Rancher version"; return 1; }
    arch="$(uname -m)"; [ "$arch" = "arm64" ] && arch=aarch64
    dmg="Rancher.Desktop-${ver}.${arch}.dmg"
    log "Downloading $dmg (verified against the published sha512)…"
    ( cd "$TD" && \
      curl -fsSLO "https://github.com/rancher-sandbox/rancher-desktop/releases/download/v${ver}/${dmg}" && \
      curl -fsSLO "https://github.com/rancher-sandbox/rancher-desktop/releases/download/v${ver}/${dmg}.sha512sum" && \
      shasum -a 512 -c "${dmg}.sha512sum" ) || return 1
    mnt="$(hdiutil attach -nobrowse "$TD/${dmg}" | tail -1 | awk -F'\t' '{print $NF}')"
    [ -n "$mnt" ] || return 1
    # curl sets no quarantine xattr → Gatekeeper stays silent; verify the signature ourselves (fail-closed)
    if ! codesign --verify --deep --strict "$mnt/Rancher Desktop.app" || ! spctl -a -t exec -vv "$mnt/Rancher Desktop.app"; then
      hdiutil detach -quiet "$mnt"
      log "Signature/Gatekeeper check failed for Rancher Desktop.app"
      return 1
    fi
    cp -R "$mnt/Rancher Desktop.app" /Applications/ 2>/dev/null \
      || sudo cp -R "$mnt/Rancher Desktop.app" /Applications/ \
      || { hdiutil detach -quiet "$mnt"; return 1; }
    hdiutil detach -quiet "$mnt"
    chmod -R a+rX "/Applications/Rancher Desktop.app" 2>/dev/null \
      || sudo chmod -R a+rX "/Applications/Rancher Desktop.app" 2>/dev/null || true   # umask 077: не запереть .app от других пользователей Mac
  fi
  # Skip the first-run GUI dialog: moby engine + kubernetes off via a deployment profile
  cat > "$HOME/Library/Preferences/io.rancherdesktop.profile.defaults.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>version</key><integer>18</integer>
<key>containerEngine</key><dict><key>name</key><string>moby</string></dict>
<key>kubernetes</key><dict><key>enabled</key><false/></dict>
</dict></plist>
EOF
  local rd="/Applications/Rancher Desktop.app/Contents/Resources/resources/darwin/bin"
  [ -x "$rd/rdctl" ] || { log "rdctl not found after install"; return 1; }
  "$rd/rdctl" set --container-engine=moby --kubernetes-enabled=false >/dev/null 2>&1 || true  # covers a pre-existing containerd config
  "$rd/rdctl" start || return 1
  PATH="$rd:$PATH"
}

install_engine_linux() {
  command -v sudo >/dev/null 2>&1 || { log "sudo is required but not available"; return 1; }
  if command -v docker >/dev/null 2>&1; then
    log "Docker CLI exists but the daemon is down — trying to start it…"
    sudo systemctl start docker 2>/dev/null || sudo service docker start 2>/dev/null || true
    DOCKER="sudo docker"   # группа docker могла ещё не примениться — проверяем через sudo
    wait_engine && return 0
    DOCKER="docker"
  fi
  log "Installing Docker Engine via the official convenience script (get.docker.com)…"
  curl -fsSL https://get.docker.com -o "$TD/get-docker.sh" || return 1
  sudo sh "$TD/get-docker.sh" || return 1
  sudo usermod -aG docker "$USER" 2>/dev/null || true   # takes effect after re-login
  DOCKER="sudo docker"                                   # this session keeps using sudo
}

# --- engine -----------------------------------------------------------------

if docker_ok; then
  FLAVOR=docker
  log "Docker is present and running."
else
  if [ "$OS" = "Darwin" ] && [ -d /Applications/Docker.app ]; then
    log "Docker Desktop found but not running — starting it…"
    open -a Docker
    if wait_engine; then FLAVOR=docker; fi
  fi
  if [ -z "$FLAVOR" ]; then
    if [ "$OS" = "Darwin" ]; then
      consent "Docker not found. Install Rancher Desktop (free, Docker-compatible, ~650 MB download; admin rights may be requested)?" \
        || python_fallback
      install_engine_macos && FLAVOR=rancher
    else
      consent "Docker not found. Install Docker Engine via the official get.docker.com script (sudo required; adds your user to the docker group — permanent root-equivalent rights)?" \
        || python_fallback
      install_engine_linux && FLAVOR=docker
    fi
    [ -n "$FLAVOR" ] || python_fallback
    log "Waiting for the docker engine (first start can take minutes)…"
    wait_engine || { log "Engine did not come up in 10 minutes"; python_fallback; }
  fi
fi

# --- config + app -------------------------------------------------------------

log "Writing .env (HOST_FP, INSTALL_FLAVOR)…"
EMPTY_SHA256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
fp="$(compute_host_fp)"
if [ -n "$fp" ] && [ "$fp" != "$EMPTY_SHA256" ]; then   # sha256 of an empty string is a constant, not an id
  upsert_env HOST_FP "$fp"
else
  log "WARN: no machine-id found — HOST_FP not set"
fi
upsert_env INSTALL_FLAVOR "$FLAVOR"
chmod 600 .env 2>/dev/null || true

log "Building and starting MaxGraph (docker compose up -d --build)…"
if ! $DOCKER compose up -d --build; then
  log "compose failed — see install.log"
  write_state error "$FLAVOR"
  exit 1
fi

ok=0
for _ in $(seq 1 60); do
  curl -fsS http://localhost:8787/healthz >/dev/null 2>&1 && { ok=1; break; }
  sleep 2
done
[ "$ok" = 1 ] || { log "healthz did not answer — check: $DOCKER compose logs maxgraph"; write_state error "$FLAVOR"; exit 1; }

write_state done "$FLAVOR"
log "MaxGraph is running (engine: $FLAVOR)."
log "Dashboard: http://localhost:8787/app · MCP: http://localhost:8787/mcp"
log "Access token: data/local_token.txt"
[ "$DOCKER" != "docker" ] && log "Note: re-login for 'docker' without sudo (group membership)."
exit 0
