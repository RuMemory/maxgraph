#!/usr/bin/env bash
# install.sh — MaxGraph installer for macOS and Linux (Windows: install.ps1).
# Run from the root of the cloned repo:  bash install.sh [--yes]
#
# Steps: working Docker? → if missing, with the user's consent install an engine
# (macOS → official Docker Desktop from docker.com; Linux → official Docker
# Engine script) → write hostfp.json + HOST_FP/INSTALL_FLAVOR into .env → pull the prebuilt
# GHCR image (local build as fallback) → up -d → wait for healthz.
#
# Modes: default = full install; --hostfp-only = only (re)compute hostfp.json
# next to docker-compose.yml (heals an existing install — no rebuild; apply with
# `docker compose up -d`).
#
# Exit codes (read by the installing AI agent, see AGENTS.md):
#   0 — MaxGraph is running in Docker (or hostfp.json written in --hostfp-only)
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
HOSTFP_ONLY=0
for a in "$@"; do case "$a" in --yes|-y) YES=1 ;; --hostfp-only) HOSTFP_ONLY=1 ;; esac; done

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

wait_engine() {  # first Docker Desktop start installs its helper and VM — allow 10 minutes
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

# --- hostfp.json: композитный отпечаток хоста --------------------------------
# Сырые идентификаторы не покидают машину: в файл идут только sha256-хеши
# (salt "maxgraph-hostfp/v1" + нормализованное значение). Тот же алгоритм —
# в install.ps1 и app/hostfp.py; нормализация и сортировка байт-в-байт
# идентичны (LC_ALL=C sort = ordinal), иначе fp расплывётся между ОС.

_HFP_SALT="maxgraph-hostfp/v1"

_hfp_sha256() {  # stdin → hex
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | awk '{print $1}';
  else shasum -a 256 | awk '{print $1}'; fi
}

_hfp_norm() {  # stdin → lowercase, trim, схлопнуть пробелы, срезать - : _
  tr '[:upper:]' '[:lower:]' | tr -d '\-:_' | tr -s '[:space:]' ' ' | sed 's/^ //; s/ $//'
}

_hfp_placeholder() {  # $1 = normalized value; 0 = плейсхолдер (в fp не идёт)
  local v="$1"
  [ -z "$v" ] && return 0
  case "$v" in
    "03000200040005000006000700080009"|"to be filled by o.e.m."|"default string"|\
    "none"|"not applicable"|"n/a"|"unknown"|"system serial number"|"serialnumber"|"oem")
      return 0 ;;
  esac
  case "$v" in *[!0]*) ;; *) return 0 ;; esac      # все-нули
  case "$v" in *[!f]*) ;; *) return 0 ;; esac      # все-f
  case "$v" in *[!0\ ]*) ;; *) return 0 ;; esac    # нули и пробелы
  return 1
}

_hfp_mac_linux() {
  local iface f
  iface="$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')"
  if [ -n "$iface" ] && [ -r "/sys/class/net/$iface/address" ]; then
    cat "/sys/class/net/$iface/address"; return
  fi
  for f in /sys/class/net/*/address; do
    case "$f" in */lo/*) continue ;; esac
    [ -r "$f" ] && { cat "$f"; return; }
  done
}

write_hostfp_json() {  # собрать компоненты ОС → hostfp.json рядом с compose
  local os_tag mid="" uuid="" board="" chassis="" serial="" mac=""
  if [ "$OS" = "Darwin" ]; then
    os_tag="macos"
    mid="$(ioreg -rd1 -c IOPlatformExpertDevice 2>/dev/null | awk -F'"' '/IOPlatformUUID/{print $4; exit}')"
    serial="$(ioreg -l 2>/dev/null | awk -F'"' '/IOPlatformSerialNumber/{print $4; exit}')"
    mac="$(ifconfig en0 2>/dev/null | awk '/ether/{print $2; exit}')"
  else
    os_tag="linux"
    mid="$(cat /etc/machine-id 2>/dev/null || cat /var/lib/dbus/machine-id 2>/dev/null)"
    uuid="$(cat /sys/class/dmi/id/product_uuid 2>/dev/null)"
    board="$(cat /sys/class/dmi/id/board_serial 2>/dev/null)"
    chassis="$(cat /sys/class/dmi/id/chassis_serial 2>/dev/null)"
    mac="$(_hfp_mac_linux)"
  fi
  local comps="" lines="" tag raw norm h
  for spec in "os_install_id:$mid" "smbios_uuid:$uuid" "board_serial:$board" \
              "chassis_serial:$chassis" "platform_serial:$serial" "disk_serial:" \
              "mac:$mac"; do
    tag="${spec%%:*}"; raw="${spec#*:}"
    [ -z "$raw" ] && continue
    norm="$(printf '%s' "$raw" | _hfp_norm)"
    _hfp_placeholder "$norm" && continue
    h="$(printf '%s' "$_HFP_SALT|$tag=$norm" | _hfp_sha256)"
    comps="${comps:+$comps,}\"$tag\":\"$h\""
    lines="${lines:+$lines
}$tag=$norm"
  done
  [ -z "$lines" ] && { log "WARN: no usable hardware ids — hostfp.json not written"; return 1; }
  local host_fp
  host_fp="$(printf '%s' "$_HFP_SALT"$'\n'"$(printf '%s\n' "$lines" | LC_ALL=C sort)" | _hfp_sha256)"
  [ -d hostfp.json ] && rmdir hostfp.json 2>/dev/null  # docker мог создать каталог-заглушку bind-монта
  printf '{"v":1,"host_fp":"%s","components":{%s},"os":"%s","created":"%s"}\n' \
    "$host_fp" "$comps" "$os_tag" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > hostfp.json \
    || { log "WARN: cannot write hostfp.json"; return 1; }
  chmod 600 hostfp.json 2>/dev/null || true
  log "hostfp.json written (machine fingerprint, hashes only)"
  return 0
}

install_engine_macos() {
  if command -v brew >/dev/null 2>&1; then
    log "Installing Docker Desktop via Homebrew…"
    brew install --cask docker || return 1
  else
    log "Homebrew not found — downloading the official Docker Desktop dmg from docker.com…"
    local arch mnt
    arch="$(uname -m)"; [ "$arch" = "arm64" ] || arch=amd64
    ( cd "$TD" && curl -fsSLo Docker.dmg "https://desktop.docker.com/mac/main/${arch}/Docker.dmg" ) || return 1
    mnt="$(hdiutil attach -nobrowse "$TD/Docker.dmg" | tail -1 | awk -F'\t' '{print $NF}')"
    [ -n "$mnt" ] || return 1
    # curl sets no quarantine xattr → Gatekeeper stays silent; verify the signature ourselves (fail-closed)
    if ! codesign --verify --deep --strict "$mnt/Docker.app" || ! spctl -a -t exec -vv "$mnt/Docker.app"; then
      hdiutil detach -quiet "$mnt"
      log "Signature/Gatekeeper check failed for Docker.app"
      return 1
    fi
    cp -R "$mnt/Docker.app" /Applications/ 2>/dev/null \
      || sudo cp -R "$mnt/Docker.app" /Applications/ \
      || { hdiutil detach -quiet "$mnt"; return 1; }
    hdiutil detach -quiet "$mnt"
    chmod -R a+rX "/Applications/Docker.app" 2>/dev/null \
      || sudo chmod -R a+rX "/Applications/Docker.app" 2>/dev/null || true   # umask 077: не запереть .app от других пользователей Mac
  fi
  log "Starting Docker Desktop (first start asks for the admin password to install its helper)…"
  open -a Docker
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

if [ "$HOSTFP_ONLY" = 1 ]; then
  # лечение существующей установки без пересборки: досчитать hostfp.json рядом
  # с compose-файлом; дальше — `docker compose up -d`, чтобы контейнер подхватил
  write_hostfp_json || exit 1
  exit 0
fi

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
      attempt=1
      while [ "$attempt" -le 2 ]; do
        if [ "$attempt" = 1 ]; then
          consent "Docker not found. Install the official Docker Desktop from docker.com (~600 MB; on first start Docker asks for your admin password to install its helper)?" || { attempt=2; continue; }
        else
          consent "Docker Desktop is the recommended engine for MaxGraph (the Python fallback loses auto-updates and autostart). Install Docker Desktop?" || break
        fi
        install_engine_macos && { FLAVOR=docker-desktop; break; }
        log "Docker Desktop did not finish installing"
        attempt=$((attempt+1))
      done
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

log "Writing .env (HOST_FP, INSTALL_FLAVOR) + hostfp.json…"
EMPTY_SHA256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
fp="$(compute_host_fp)"
if [ -n "$fp" ] && [ "$fp" != "$EMPTY_SHA256" ]; then   # sha256 of an empty string is a constant, not an id
  upsert_env HOST_FP "$fp"
else
  log "WARN: no machine-id found — HOST_FP not set"
fi
upsert_env INSTALL_FLAVOR "$FLAVOR"
write_hostfp_json || true   # отпечаток не критичен для запуска: без него установка unanchored
mgv="$(cat VERSION 2>/dev/null || true)"
if ! printf '%s' "$mgv" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then   # мусор в VERSION не должен отравить ref образа
  [ -n "$mgv" ] && log "WARN: VERSION is not semver ('$mgv') — MG_VERSION empty (image: latest)"
  mgv=""
fi
upsert_env MG_VERSION "$mgv"   # pin готового образа GHCR; пусто → latest
chmod 600 .env 2>/dev/null || true

running="$($DOCKER exec maxgraph cat /app/VERSION 2>/dev/null || true)"
printf '%s' "$running" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$' || running=""   # мусор/чужой контейнер → гвард молчит
lowest="$(printf '%s\n%s\n' "$running" "$mgv" | sort -t. -k1,1n -k2,2n -k3,3n | head -1)"   # BSD/macOS: sort -V нет
if [ -n "$running" ] && [ -n "$mgv" ] && [ "$running" != "$mgv" ] \
   && [ "$lowest" = "$mgv" ]; then
  upsert_env MG_VERSION "$running"   # автообновлённая установка + старый клон: даунгрейдить нельзя
  log "Running MaxGraph $running is NEWER than this clone ($mgv) — the clone is stale (git pull for a fresh one). Leaving the running server untouched."
  write_state done "$FLAVOR"
  exit 0
fi

log "Starting MaxGraph (pulling the prebuilt image first; local build is the fallback)…"
if ! $DOCKER compose pull >/dev/null 2>&1; then
  log "Prebuilt image unavailable — building locally (a few minutes)…"
fi
if ! $DOCKER compose up -d; then
  log "up failed — retrying with a full local build…"
  if ! $DOCKER compose up -d --build; then
    log "compose failed — see install.log"
    write_state error "$FLAVOR"
    exit 1
  fi
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
