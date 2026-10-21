#!/usr/bin/env bash
# install-native.sh — MaxGraph NATIVE installer for macOS and Linux (no Docker).
# The buyer's path: download the portable binary from the store, verify it, run it.
# Run from the root of the cloned repo:
#   bash install-native.sh [--yes] [--project-dir <user project root>] [--rules-lang ru|en]
#
# Steps: store manifest -> pick <os>-<arch> -> download the binary + .sha256
# sidecar + .sig (atomic: .part -> verify -> rename) into the install dir
# (Linux: ~/.local/share/maxgraph, macOS: ~/Applications/maxgraph) -> create the
# app shortcut (Linux: ~/.local/share/applications/maxgraph.desktop; macOS:
# ~/Applications/MaxGraph symlink) -> verify
# sha256 from the manifest (sha256sum / shasum -a 256) -> verify the ed25519
# signature via OpenSSL 3 against the embedded release pubkey -> chmod +x ->
# stop an old maxgraph process on reinstall -> start via nohup -> wait for
# http://localhost:8787/healthz (up to 30s) -> read the token from
# data/local_token.txt -> inject the memory rules into <project-dir>/AGENTS.md
# (idempotent, <!-- maxgraph:start/end -->) -> write/register capture hooks ->
# print the next-steps package (MAXGRAPH_NEXT_STEPS={...}).
#
# Trust chain (owner's decision, no code-signing certificates):
#   install time  = HTTPS + sha256 from the store manifest + ed25519 signature
#   verified via OpenSSL 3 (trust anchor embedded below — NOT downloaded from
#   the same host). Fail-closed: no OpenSSL 3 / bad signature -> exit 1.
#   Windows (install-native.ps1) keeps sha256+HTTPS only: Windows has no
#   OpenSSL out of the box (owner informed, security review 2026-10-18).
#   The binary also verifies ed25519 itself on every auto-update.
# curl does NOT set the macOS quarantine xattr -> Gatekeeper stays silent.
#
# MG_TEST_PUBKEY_FILE (tests only): path to an override ed25519 PEM pubkey
# for verify_ed25519; production installs use the embedded key.
#
# Extra flags: --store-url <url> (default https://rezon-ai.ru; tests point it
# at a mock), --no-start (everything except launching the binary — for tests),
# --install-dir <dir> (overrides the default install dir).
#
# Exit codes: 0 — installed and running (or --no-start completed); 1 — error
# (see install-native.log). Re-run = update to the latest version (idempotent).
set -uo pipefail
umask 077   # downloads and the token must not be world-readable
cd "$(dirname "$0")"

LOG=install-native.log
YES=0
NO_START=0
PROJECT_DIR=""
RULES_LANG=""
RULES_INJECTED=0
STORE_URL="https://rezon-ai.ru"
INSTALL_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1 ;;
    --no-start) NO_START=1 ;;
    --project-dir) shift
      case "${1:-}" in -*|"") echo "[maxgraph-install] FATAL: --project-dir needs a path" >&2; exit 1 ;;
        *) PROJECT_DIR="$1" ;; esac ;;
    --project-dir=*) PROJECT_DIR="${1#*=}" ;;
    --rules-lang) shift
      case "${1:-}" in -*|"") echo "[maxgraph-install] FATAL: --rules-lang needs ru|en" >&2; exit 1 ;;
        *) RULES_LANG="$1" ;; esac ;;
    --rules-lang=*) RULES_LANG="${1#*=}" ;;
    --store-url) shift
      case "${1:-}" in -*|"") echo "[maxgraph-install] FATAL: --store-url needs a URL" >&2; exit 1 ;;
        *) STORE_URL="$1" ;; esac ;;
    --store-url=*) STORE_URL="${1#*=}" ;;
    --install-dir) shift
      case "${1:-}" in -*|"") echo "[maxgraph-install] FATAL: --install-dir needs a path" >&2; exit 1 ;;
        *) INSTALL_DIR="$1" ;; esac ;;
    --install-dir=*) INSTALL_DIR="${1#*=}" ;;
    *) echo "[maxgraph-install] FATAL: unknown flag '$1'" >&2; exit 1 ;;
  esac
  shift
done

if [ "${MG_INSTALL_LIB_ONLY:-0}" != "1" ]; then   # тесты source'ят функции даже на Windows
OS="$(uname -s)"
case "$OS" in Darwin|Linux) ;; *)
  echo "[maxgraph-install] Unsupported OS: $OS (on Windows use install-native.ps1)"; exit 1 ;;
esac
ARCH="$(uname -m)"
case "$ARCH" in x86_64|amd64) ARCH=amd64 ;; arm64|aarch64) ARCH=arm64 ;; *)
  echo "[maxgraph-install] Unsupported CPU arch: $ARCH"; exit 1 ;;
esac
if [ "$OS" = "Darwin" ]; then MG_OS=macos; else MG_OS=linux; fi
if [ -z "$INSTALL_DIR" ]; then
  if [ "$OS" = "Darwin" ]; then INSTALL_DIR="$HOME/Applications/maxgraph"
  else INSTALL_DIR="$HOME/.local/share/maxgraph"; fi
fi
STORE_URL="${STORE_URL%/}"
TOKEN_FILE="$INSTALL_DIR/data/local_token.txt"   # the binary creates ./data next to itself
fi

log() { echo "[maxgraph-install] $*" | tee -a "$LOG"; }
fail() { log "ERROR: $*"; exit 1; }

# --- memory rules into the user's project (--project-dir) --------------------
# Скопировано из install.sh без изменений: маркеры <!-- maxgraph:start/end -->,
# идемпотентно, переносы строк файла наследуются, запись атомарная.

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

# --- hooks (как в install.sh, но токен читается из $INSTALL_DIR/data) ---------

write_hook_files() {  # ~/.maxgraph/: хук + hooks.env; регистрацию в конфигах
  # хостов делает агент по docs/HOOKS.md. hooks.env — единственное место с
  # токеном (chmod 600), пишется один раз (не затираем ручные правки).
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
  [ -f "$TOKEN_FILE" ] && token="$(tr -d '[:space:]' < "$TOKEN_FILE")"
  if [ ! -f "$d/hooks.env" ] && [ -n "$token" ]; then
    ( umask 077; { printf 'MG_SERVER=http://localhost:8787\n'; printf 'MG_TOKEN=%s\n' "$token"; } \
      > "$d/hooks.env" )   # 600 с рождения: токен недолжен быть читаем даже на секунду
  fi
  log "Hooks ready: ~/.maxgraph (agent registers them into its host — docs/HOOKS.md)"
}

register_hooks() {  # авто-регистрация хуков во всех НАЙДЕННЫХ хостах (молча; агент — фолбэк)
  [ -f hooks/register_hook.py ] || return 1
  if ! command -v python3 >/dev/null 2>&1; then
    log "python3 not found — hooks not registered automatically (agent registers by docs/HOOKS.md)"
    return 1
  fi
  python3 hooks/register_hook.py --auto | while IFS= read -r line; do log "hooks: $line"; done
}

print_next_steps() {  # финальный пакет для агента-установщика (см. AGENTS.md)
  local token="" cfg=""
  [ -f "$TOKEN_FILE" ] && token="$(tr -d '[:space:]' < "$TOKEN_FILE")"
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
   DSH: already done — row mcp-maxgraph in ~/.dsh/profiles/*/cordis.patch.yml (see "hooks:" log lines)
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
  # машиночитаемая строка — ТОЛЬКО stdout; в install-native.log токен не пишем
  # (его прикрепляют к багрепортам). json_escape: пути с \ и " ломали JSON.
  printf 'MAXGRAPH_NEXT_STEPS={"mcp_url":"http://localhost:8787/mcp","token":"%s","dashboard":"http://localhost:8787/app#%s","rules_injected":%s,"project_dir":"%s"}\n' \
    "$(json_escape "$token")" "$(json_escape "$token")" "$([ "$RULES_INJECTED" = 1 ] && printf true || printf false)" \
    "$(json_escape "$PROJECT_DIR")"
  printf 'MAXGRAPH_NEXT_STEPS: rules_injected=%s project_dir=%s (token — только stdout)\n' \
    "$([ "$RULES_INJECTED" = 1 ] && printf true || printf false)" "$PROJECT_DIR" >> "$LOG"
}

# --- native download ----------------------------------------------------------

_sha256_file() {  # $1 = path → hex
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

# --- ed25519 verify (M-1, security review 2026-10-18) ------------------------
# Боевой release-ключ ed25519 (trust anchor). ВШИТ константой — trust anchor
# НЕЛЬЗЯ качать с того же хоста, что и артефакт. Копия app/release.pub.pem;
# при ротации ключа обновить И ЗДЕСЬ (и перевыпустить установщик).
MG_RELEASE_PUB_PEM='-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAxFQmmGesTXd9ynmG4C2dF/aWb8RCm5yD3jgA7AzkIHI=
-----END PUBLIC KEY-----'

verify_ed25519() {  # $1 = файл, $2 = .sig; fail-closed (fail → exit 1)
  command -v openssl >/dev/null 2>&1 \
    || fail "OpenSSL not found - cannot verify the ed25519 signature. Install OpenSSL 3+ (macOS: brew install openssl) and re-run."
  local over
  over="$(openssl version 2>/dev/null | awk '{print $2}')"
  case "$over" in 3.*|[4-9].*) ;; *)
    fail "OpenSSL >= 3 required for ed25519 verify (found: ${over:-unknown}; macOS ships LibreSSL - use: brew install openssl)." ;;
  esac
  local pubf tmpf=""
  if [ -n "${MG_TEST_PUBKEY_FILE:-}" ]; then
    pubf="$MG_TEST_PUBKEY_FILE"   # тестовый ключ (НЕ для прода)
  else
    tmpf="$(mktemp "${TMPDIR:-/tmp}/mg-pub.XXXXXX.pem")" \
      || fail "cannot create a temp file for the release pubkey"
    printf '%s\n' "$MG_RELEASE_PUB_PEM" > "$tmpf" || { rm -f "$tmpf"; fail "cannot write the release pubkey"; }
    pubf="$tmpf"
  fi
  if ! openssl pkeyutl -verify -pubin -inkey "$pubf" -rawin -in "$1" -sigfile "$2" >/dev/null 2>&1; then
    [ -n "$tmpf" ] && rm -f "$tmpf"
    log "ERROR: ed25519 signature INVALID for $1"
    return 1   # не fail(): вызывающий чистит .part и падает сам
  fi
  [ -n "$tmpf" ] && rm -f "$tmpf"
  log "ed25519 signature verified (release key)."
}

fetch_manifest() {  # выставляет глобальный MANIFEST (fail внутри command
  # substitution убил бы только subshell — поэтому без stdout-контракта)
  local url="$STORE_URL/api/releases/native/manifest"
  local body code
  body="$(curl -fsS -w '\n%{http_code}' "$url" 2>/dev/null)" || true
  code="$(printf '%s' "$body" | tail -1)"
  body="$(printf '%s' "$body" | sed '$d')"
  case "$code" in
    200) MANIFEST="$body" ;;
    404) fail "Store manifest not found (404): $url" ;;
    *)   fail "Cannot fetch the store manifest ($url): HTTP $code" ;;
  esac
}

# Манифест машинно-генерируемый и плоский: {"version":"X.Y.Z","artifacts":[
# {"name":"...","os":"...","arch":"...","bytes":N,"sha256":"..."},...]}.
# Парсим без jq (его может не быть): схлопываем whitespace и вырезаем объект
# с нужными os/arch. Если формат магазина усложнится — здесь чинить.
select_artifact() {  # $1 = manifest → выставляет ART_NAME / ART_SHA / MG_VERSION
  local norm entry
  norm="$(printf '%s' "$1" | tr -d ' \t\r\n')"
  MG_VERSION="$(printf '%s' "$norm" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p' | head -1)"
  printf '%s' "$MG_VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$' \
    || fail "Store manifest has no valid semver 'version' ('$MG_VERSION')"
  entry="$(printf '%s' "$norm" | grep -oE '\{[^{}]*"os":"'"$MG_OS"'"[^{}]*\}' | grep '"arch":"'"$ARCH"'"' | head -1)"
  [ -n "$entry" ] || fail "No $MG_OS-$ARCH artifact in the store manifest (version $MG_VERSION)"
  ART_NAME="$(printf '%s' "$entry" | sed -n 's/.*"name":"\([^"]*\)".*/\1/p')"
  ART_SHA="$(printf '%s' "$entry" | sed -n 's/.*"sha256":"\([^"]*\)".*/\1/p' | tr 'A-F' 'a-f')"
  case "$ART_NAME" in *[!A-Za-z0-9._-]*|"") fail "Artifact name is unsafe: '$ART_NAME'" ;; esac   # path-traversal guard
  printf '%s' "$ART_SHA" | grep -qE '^[0-9a-f]{64}$' || fail "Artifact $ART_NAME: sha256 is missing or malformed"
}

download_verified() {  # выставляет глобальный BIN (log пишет в stdout —
  # command substitution здесь был бы ловушкой)
  # Атомарно: .part -> проверка sha256 из манифеста -> rename. Обрыв закачки
  # оставляет .part, а не «полурабочий» бинарь; rename после верификации.
  local bin="$INSTALL_DIR/$ART_NAME" part="$INSTALL_DIR/$ART_NAME.part" url="$STORE_URL/api/releases/native/$ART_NAME"
  mkdir -p "$INSTALL_DIR" || fail "Cannot create $INSTALL_DIR"
  log "Downloading $ART_NAME (version $MG_VERSION)…"
  local code
  code="$(curl -fsS -o "$part" -w '%{http_code}' "$url" 2>/dev/null)" || code="000"
  case "$code" in
    200) ;;
    404) rm -f "$part"; fail "Artifact not found on the store (404): $url" ;;
    *)   rm -f "$part"; fail "Download failed ($url): HTTP $code" ;;
  esac
  local actual
  actual="$(_sha256_file "$part")"
  if [ "$actual" != "$ART_SHA" ]; then
    rm -f "$part"
    fail "sha256 mismatch for $ART_NAME: expected $ART_SHA, got $actual"
  fi
  log "sha256 verified against the store manifest."
  # ed25519-подпись (.sig) — обязательна (M-1): trust anchor вшит выше.
  code="$(curl -fsS -o "$part.sig" -w '%{http_code}' "$url.sig" 2>/dev/null)" || code="000"
  if [ "$code" != "200" ]; then
    rm -f "$part" "$part.sig"
    fail "Signature not found on the store (HTTP $code): $url.sig"
  fi
  if ! verify_ed25519 "$part" "$part.sig"; then
    rm -f "$part" "$part.sig"
    fail "ed25519 signature verification failed for $ART_NAME - refusing to install."
  fi
  rm -f "$part.sig"
  # sidecar .sha256 (формат sha256sum) — для ручных проверок пользователем; best effort
  curl -fsS -o "$bin.sha256" "$url.sha256" 2>/dev/null || rm -f "$bin.sha256"
  # Переустановка: гасим старый maxgraph-процесс до замены файла.
  # MINOR-3: ТОЧНЫЙ матч по argv[0] ("./$ART_NAME" из $INSTALL_DIR или полный
  # путь) — pgrep -f по подстроке пути убивал чужие процессы с похожей командой.
  local old_pids
  old_pids="$(ps -eo pid=,args= 2>/dev/null | awk -v a="./$ART_NAME" -v b="$bin" '$2 == a || $2 == b {print $1}')"
  if [ -n "$old_pids" ]; then
    log "Stopping the running MaxGraph for the update…"
    kill $old_pids 2>/dev/null || true
    sleep 2
    kill -9 $old_pids 2>/dev/null || true
  fi
  chmod +x "$part"
  mv -f "$part" "$bin" || { rm -f "$part"; fail "Cannot install to $bin"; }
  log "Installed to $bin"
  BIN="$bin"
}

# --- app shortcuts (та же логика, что native.ensure_shortcuts; exe повторит её
# и при старте — но по конвенции install.ps1 ярлык должен появиться сразу) ----
create_shortcuts() {  # $1 = полный путь к exe; best effort (вызывающий — || true)
  local exe="$1"
  if [ "$MG_OS" = "macos" ]; then
    mkdir -p "$HOME/Applications" || { log "WARN: cannot create ~/Applications"; return 1; }
    # -sfn: пересоздаёт битый/чужой симлинк; обычный файл на месте — тоже заменяем
    rm -f "$HOME/Applications/MaxGraph" 2>/dev/null || true
    ln -s "$exe" "$HOME/Applications/MaxGraph" || { log "WARN: symlink failed"; return 1; }
    log "shortcut: $HOME/Applications/MaxGraph -> $exe"
  else
    local apps="$HOME/.local/share/applications"
    mkdir -p "$apps" || { log "WARN: cannot create $apps"; return 1; }
    cat > "$apps/maxgraph.desktop" <<EOF || { log "WARN: cannot write $apps/maxgraph.desktop"; return 1; }
[Desktop Entry]
Type=Application
Name=MaxGraph
Exec=$exe
Icon=$exe
Terminal=false
Categories=Utility
EOF
    log "shortcut: $apps/maxgraph.desktop"
  fi
}

wait_healthz() {  # до 30 секунд; строгое «ok» — не подстрока (MINOR-5)
  local i body
  for i in $(seq 1 15); do
    body="$(curl -fsS http://localhost:8787/healthz 2>/dev/null | tr -d '[:space:]')"
    [ "$body" = "ok" ] && return 0
    sleep 2
  done
  return 1
}

# --- main ---------------------------------------------------------------------

# MG_INSTALL_LIB_ONLY=1 — только функции (тесты source'ят скрипт и дёргают
# verify_ed25519 с MG_TEST_PUBKEY_FILE; боевой запуск не трогаем).
if [ "${MG_INSTALL_LIB_ONLY:-0}" = "1" ]; then return 0 2>/dev/null || exit 0; fi

log "Store: $STORE_URL; install dir: $INSTALL_DIR ($MG_OS-$ARCH)"
MANIFEST=""
BIN=""
fetch_manifest
select_artifact "$MANIFEST"
download_verified
bin="$BIN"
create_shortcuts "$bin" || true   # ярлык не критичен; exe при старте повторит ensure_shortcuts

if [ "$NO_START" = 1 ]; then
  log "no-start: skipping the binary launch, healthz wait and token read (test mode)."
else
  log "Starting MaxGraph (nohup, detached)…"
  ( cd "$INSTALL_DIR" && nohup "./$ART_NAME" >/dev/null 2>&1 & )
  wait_healthz || { log "healthz did not answer within 30s — check the binary output in $INSTALL_DIR"; exit 1; }
  log "MaxGraph is up (http://localhost:8787)."
fi

inject_rules || true   # правила не критичны для запуска; предупреждение уже в логе
write_hook_files || true
register_hooks || true
log "MaxGraph native installed (version $MG_VERSION)."
print_next_steps
exit 0
