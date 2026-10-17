#!/usr/bin/env bash
# install.sh — MaxGraph installer for macOS and Linux (Windows: install.ps1).
# Run from the root of the cloned repo:
#   bash install.sh [--yes] [--project-dir <user project root>] [--rules-lang ru|en]
#
# Steps: working Docker? → if missing, with the user's consent install an engine
# (macOS → official Docker Desktop from docker.com; Linux → official Docker
# Engine script) → write hostfp.json + HOST_FP/INSTALL_FLAVOR into .env → pull the prebuilt
# GHCR image (local build as fallback) → up -d → wait for healthz → print the
# next-steps package (token + MCP config) for the installing AI agent.
# --project-dir: the memory rules block is injected into <dir>/AGENTS.md between
# <!-- maxgraph:start/end --> markers (idempotent).
#
# Modes: default = full install; --hostfp-only = only (re)compute hostfp.json
# next to docker-compose.yml (heals an existing install — no rebuild; apply with
# `docker compose up -d`; no rules injection, no package print).
#
# Exit codes (read by the installing AI agent, see AGENTS.md):
#   0 — MaxGraph is running in Docker (or hostfp.json written in --hostfp-only)
#   3 — engine declined or impossible → Python fallback (AGENTS.md, step 1a)
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
PROJECT_DIR=""
RULES_LANG=""
RULES_INJECTED=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1 ;;
    --hostfp-only) HOSTFP_ONLY=1 ;;
    --project-dir) shift
      case "${1:-}" in -*|"") echo "[maxgraph-install] FATAL: --project-dir needs a path" >&2; exit 1 ;;
        *) PROJECT_DIR="$1" ;; esac ;;
    --project-dir=*) PROJECT_DIR="${1#*=}" ;;
    --rules-lang) shift
      case "${1:-}" in -*|"") echo "[maxgraph-install] FATAL: --rules-lang needs ru|en" >&2; exit 1 ;;
        *) RULES_LANG="$1" ;; esac ;;
    --rules-lang=*) RULES_LANG="${1#*=}" ;;
  esac
  shift
done

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

# --- memory rules into the user's project (--project-dir) --------------------
# Injected BEFORE any engine work: only the repo file is needed, and the block
# must land on every exit path (including the Python fallback). Idempotent:
# the <!-- maxgraph:start/end --> markers are replaced in place on re-runs.

rules_lang() {  # explicit flag wins; otherwise the OS locale decides
  case "$RULES_LANG" in
    ru|en) printf '%s' "$RULES_LANG"; return ;;
  esac
  case "${LANG:-}${LC_ALL:-}" in ru*|*ru_RU*) printf 'ru' ;; *) printf 'en' ;; esac
}

json_escape() {  # минимальное JSON-экранирование строки (пути с " и \ ломали MAXGRAPH_NEXT_STEPS)
  local s="$1"
  s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//$'\n'/\\n}"; s="${s//$'\r'/\\r}"; s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

extract_rules_block() {  # $1 = ru|en → block from maxgraph-agent.md (single source of truth)
  local hdr
  case "$1" in ru) hdr='## Блок (RU' ;; *) hdr='## Блок (EN' ;; esac
  awk -v hdr="$hdr" '
    !inhdr && index($0, hdr) == 1 { inhdr=1; next }
    inhdr && /^```/ && !inblock { inblock=1; next }
    inblock && /^```/ { exit }
    inblock { print }
  ' maxgraph-agent.md
}

inject_rules() {
  [ -n "$PROJECT_DIR" ] || return 0
  case "$RULES_LANG" in ru|en|"") ;; *) log "WARN: --rules-lang '$RULES_LANG' — ожидается ru|en; определяю по локали"; RULES_LANG="" ;; esac
  local dir f block
  dir="$(cd "$PROJECT_DIR" 2>/dev/null && pwd -P)" \
    || { log "WARN: --project-dir '$PROJECT_DIR' does not exist — rules not injected"; return 1; }
  if [ "$dir" = "$(pwd -P)" ]; then
    log "WARN: --project-dir points at the cloned repo itself — rules belong in the USER's project root; skipped"
    return 1
  fi
  block="$(extract_rules_block "$(rules_lang)")"
  [ -n "$block" ] || { log "WARN: rules block not found in maxgraph-agent.md"; return 1; }
  f="$dir/AGENTS.md"
  [ -L "$f" ] && { log "WARN: $f — симлинк; запись сквозь него отклонена"; return 1; }
  # Маркер — ТОЛЬКО строкой целиком (инлайн-упоминание маркеров внутри самого
  # блока не должно срабатывать); grep-чек быстрый и мягкий, строгое равенство
  # строк — в awk; битая пара (end раньше start / нет end) → файл не трогаем.
  # CRLF-файл → и блок в CRLF, чтобы не смешивать переносы.
  local crlf=0 tmp
  [ -f "$f" ] && grep -q $'\r' "$f" && crlf=1
  [ "$crlf" = 1 ] && block="$(printf '%s\n' "$block" | sed 's/$/\r/')"
  if [ -f "$f" ] && grep -qF '<!-- maxgraph:start -->' "$f" && grep -qF '<!-- maxgraph:end -->' "$f"; then
    tmp="$(mktemp "$dir/.mg-agents.XXXXXX")" || { log "WARN: cannot create temp file in $dir"; return 1; }
    BLOCK="$block" awk '
      { line = $0; sub(/\r$/, "", line) }
      line == "<!-- maxgraph:start -->" && !done { print; print ENVIRON["BLOCK"]; skip=1; done=1; next }
      line == "<!-- maxgraph:end -->" && skip { skip=0; print; next }
      !skip { print }
      END { if (skip || !done) exit 3 }
    ' "$f" > "$tmp" \
      || { rm -f "$tmp"; log "WARN: cannot update $f (битая пара маркеров?) — файл не тронут"; return 1; }
    local mode
    mode="$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f" 2>/dev/null)"
    [ -n "$mode" ] && chmod "$mode" "$tmp" 2>/dev/null   # mv пересоздаёт файл; umask 077 иначе даст 600
    mv -f "$tmp" "$f" || { rm -f "$tmp"; log "WARN: cannot replace $f"; return 1; }
    log "Memory rules updated in $f (between markers)"
  else
    if [ "$crlf" = 1 ]; then
      { [ -s "$f" ] && printf '\r\n'; printf '<!-- maxgraph:start -->\r\n%s\n<!-- maxgraph:end -->\r\n' "$block"; } >> "$f" \
        || { log "WARN: cannot write $f"; return 1; }
    else
      { [ -f "$f" ] && printf '\n'; printf '<!-- maxgraph:start -->\n%s\n<!-- maxgraph:end -->\n' "$block"; } >> "$f" \
        || { log "WARN: cannot write $f"; return 1; }
    fi
    log "Memory rules written into $f"
  fi
  RULES_INJECTED=1
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

write_hook_files() {  # ~/.maxgraph/: хук + hooks.env; регистрацию в конфигах
  # хостов делает агент по docs/HOOKS.md (знает свой хост; вопросов про другие
  # нейросети не задаём). hooks.env — единственное место с токеном (chmod 600),
  # пишется один раз (не затираем ручные правки); хук обновляем всегда.
  local d="$HOME/.maxgraph"
  [ -L "$d" ] && { log "WARN: $d is a symlink — refusing to write hooks.env"; return 1; }
  mkdir -p "$d" || { log "WARN: cannot create $d"; return 1; }
  if [ -f hooks/maxgraph_hook.py ]; then
    cp hooks/maxgraph_hook.py "$d/maxgraph_hook.py" \
      || { log "WARN: hook copy failed"; return 1; }
  else
    log "WARN: hooks/maxgraph_hook.py not found in the clone"; return 1
  fi
  local token=""
  [ -f data/local_token.txt ] && token="$(tr -d '[:space:]' < data/local_token.txt)"
  if [ ! -f "$d/hooks.env" ] && [ -n "$token" ]; then
    ( umask 077; { printf 'MG_SERVER=http://localhost:8787\n'; printf 'MG_TOKEN=%s\n' "$token"; } \
      > "$d/hooks.env" )   # 600 с рождения: токен недолжен быть читаем даже на секунду
  fi
  log "Hooks ready: ~/.maxgraph (agent registers them into its host — docs/HOOKS.md)"
}

register_hooks() {  # авто-регистрация хуков во всех НАЙДЕННЫХ хостах (молча,
  # без вопросов про другие программы; агент — фолбэк для нестандартных хостов)
  [ -f hooks/register_hook.py ] || return 1
  if ! command -v python3 >/dev/null 2>&1; then
    log "python3 not found — hooks not registered automatically (agent registers by docs/HOOKS.md)"
    return 1
  fi
  python3 hooks/register_hook.py --auto | while IFS= read -r line; do log "hooks: $line"; done
}

print_next_steps() {  # финальный пакет для агента-установщика (см. AGENTS.md)
  local token="" cfg=""
  [ -f data/local_token.txt ] && token="$(tr -d '[:space:]' < data/local_token.txt)"
  [ -n "$token" ] || token="<см. data/local_token.txt>"
  cfg="\"url\":\"http://localhost:8787/mcp\",\"headers\":{\"Authorization\":\"Bearer $token\"}"
  local rules_text
  if [ "$RULES_INJECTED" = 1 ]; then
    rules_text="already in the project AGENTS.md ($PROJECT_DIR)"
  else
    rules_text="NOT injected — paste the block from maxgraph-agent.md between <!-- maxgraph:start --> and <!-- maxgraph:end --> in the project AGENTS.md"
  fi
  cat <<EOF

================ MAXGRAPH — NEXT STEPS (for the agent) ================
1. Connect MCP to the user's host:
   url: http://localhost:8787/mcp
   header: Authorization: Bearer $token
   JSON config (any host): {"mcpServers":{"maxgraph":{"type":"http",$cfg}}}
   Claude Code: claude plugin marketplace add RuMemory/maxgraph && claude plugin install maxgraph@maxgraph
2. Memory rules: $rules_text
3. Capture hooks: auto-registered into every detected agent host (see "hooks:" log lines); if yours was skipped — one snippet from docs/HOOKS.md.
4. Verify memory: create_entities install-check → search_nodes install-check.
5. Tell the user: fully restart the host and start a NEW session in the project folder.
   Dashboard: http://localhost:8787/app#$token (token is in the link — opens signed in).
=======================================================================
EOF
  # машиночитаемая строка — ТОЛЬКО stdout; в install.log токен не пишем (его
  # прикрепляют к багрепортам). json_escape: пути с \ и " ломали JSON.
  printf 'MAXGRAPH_NEXT_STEPS={"mcp_url":"http://localhost:8787/mcp","token":"%s","dashboard":"http://localhost:8787/app#%s","rules_injected":%s,"project_dir":"%s"}\n' \
    "$(json_escape "$token")" "$(json_escape "$token")" "$([ "$RULES_INJECTED" = 1 ] && printf true || printf false)" \
    "$(json_escape "$PROJECT_DIR")"
  printf 'MAXGRAPH_NEXT_STEPS: rules_injected=%s project_dir=%s (token — только stdout)\n' \
    "$([ "$RULES_INJECTED" = 1 ] && printf true || printf false)" "$PROJECT_DIR" >> "$LOG"
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

inject_rules || true   # правила не критичны для запуска; предупреждение уже в логе

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
  write_hook_files || true
  register_hooks || true
  write_state done "$FLAVOR"
  print_next_steps
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

write_hook_files || true   # хуки не критичны для запуска; агент зарегистрирует по docs/HOOKS.md
register_hooks || true     # авто-регистрация в найденных хостах (не роняет установку)
write_state done "$FLAVOR"
log "MaxGraph is running (engine: $FLAVOR)."
print_next_steps
[ "$DOCKER" != "docker" ] && log "Note: re-login for 'docker' without sudo (group membership)."
exit 0
