#!/bin/sh



























umask 077
set -u

STORE_URL="${STORE_URL:-https://rezon-ai.ru}"
CHECK_INTERVAL="${CHECK_INTERVAL:-3600}"
AUTO_UPDATE="${AUTO_UPDATE:-1}"
TARGET_CONTAINER="${TARGET_CONTAINER:-maxgraph}"
SELF_CONTAINER="${SELF_CONTAINER:-maxgraph-updater}"
APP_URL="${APP_URL:-http://$TARGET_CONTAINER:8787}"
STATE_DIR="${STATE_DIR:-/state}"
STATE_FILE="$STATE_DIR/update_state.json"
TARGET_IMAGE="${TARGET_IMAGE:-maxgraph-local}"
SELF_IMAGE="${SELF_IMAGE:-maxgraph-updater}"
RUN_ONCE="${RUN_ONCE:-0}"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') updater: $*"; }


ver_norm() {
    case "$1" in ""|*[!0-9.]*) echo ""; return ;; esac
    v="$1"
    while [ "$(echo "$v" | awk -F. '{print NF}')" -lt 3 ]; do v="$v.0"; done
    echo "$v"
}


ver_newer() {
    a=$(ver_norm "$1"); b=$(ver_norm "$2")
    [ -n "$a" ] && [ -n "$b" ] || return 1
    [ "$a" = "$b" ] && return 1
    [ "$(printf '%s\n%s\n' "$b" "$a" | sort -t. -k1,1n -k2,2n -k3,3n | head -1)" = "$b" ]
}

fetch_latest() {
    curl -fsS --max-time 20 --max-filesize 4096 "$STORE_URL/api/version" 2>/dev/null \
        | jq -r '.version // empty' 2>/dev/null
}

current_version() {

    docker exec "$TARGET_CONTAINER" sh -c 'cat /app/VERSION 2>/dev/null' 2>/dev/null \
        | tr -dc '0-9.'
}

write_state() {
    mkdir -p "$STATE_DIR" 2>/dev/null || true
    printf '{"ts":"%s","current":"%s","latest":"%s","result":"%s","note":"%s","prev_image":"%s"}\n' \
        "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$(current_version)" \
        "$2" "$1" "$3" "${PREV_IMAGE:-}" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
}

healthcheck() {
    i=0
    while [ $i -lt 60 ]; do
        if curl -fsS --max-time 2 "$APP_URL/healthz" 2>/dev/null | grep -q ok; then
            return 0
        fi
        i=$((i + 1)); sleep 2
    done
    return 1
}


archive_safe() {
    tar tzf "$1" >/dev/null 2>&1 || return 1
    tar tzvf "$1" 2>/dev/null | awk 'substr($1,1,1) != "-" && substr($1,1,1) != "d" { bad=1 } END { exit bad }' || return 1
    ! tar tzf "$1" | grep -qE '^/|(^|/)\.\.(/|$)'
}

















clone_recreate() {
    name="$1"; image="$2"; rb="$3"
    cj=$(mktemp) || return 1
    docker inspect "$name" > "$cj" 2>/dev/null || {
        log "ОШИБКА: inspect $name"; rm -f "$cj"; return 1; }






    set -- --name "$name"
    : > "$cj.nets"
    while IFS= read -r a; do
        case "$a" in
            NET"	"*) printf '%s\n' "$a" >> "$cj.nets" ;;
            REJ"	"*) log "ПРОПУЩЕНО: $(printf '%s' "$a" | cut -f2-)" ;;
            *) set -- "$@" "$a" ;;
        esac
    done <<EOF
$(jq -r '
  def clean: gsub("[\r\n]"; " ");
  .[0] as $c
  | (($c.Config.Env // [])[] | "--env", (. | clean)),
    (($c.Config.Labels // {}) | to_entries[] | "--label", ((.key + "=" + .value) | clean)),
    (($c.Mounts // [])[]
      # Запятая в пути инжектит ключи в csv-парсер
      # --mount (last-wins) — отклоняем весь маунт, лог через REJ-строку.
      # Volume без .Name дал бы source= → анонимный том — отклоняем.
      | if ([.Type // "", .Source // "", .Name // "", .Destination // ""]
            | map(contains(",")) | any) then
          "REJ\tмаунт с запятой в пути отклонён: " + (.Destination // "?")
        elif .Type == "volume" and .Name == null then
          "REJ\tvolume-маунт без имени отклонён: " + (.Destination // "?")
        else
          "--mount",
          (("type=" + .Type + ",source=" + (if .Type == "volume" then .Name else .Source end)
            + ",target=" + .Destination
            + (if .RW == false then ",readonly" else "" end)) | clean)
        end),
    (($c.HostConfig.PortBindings // {}) | to_entries[] | .key as $p | .value[] |
      "-p", (((if .HostIp != null and .HostIp != "" then .HostIp + ":" else "" end)
             + .HostPort + ":" + $p) | clean)),
    ("--restart", (($c.HostConfig.RestartPolicy.Name // "unless-stopped") | clean)),
    (if ($c.Config.WorkingDir // "") != "" then "--workdir", ($c.Config.WorkingDir | clean) else empty end),
    (if ($c.Config.User // "") != "" then "--user", ($c.Config.User | clean) else empty end),
    (($c.NetworkSettings.Networks // {}) | to_entries[]
      | (("NET\t" + .key + "\t" + (((.value.Aliases // [])
          | map(select(. != $c.Name and . != $c.Id and . != ($c.Id[0:12])))) | join(",")))
         | clean))
' "$cj" 2>/dev/null)
EOF
    if [ $# -le 2 ]; then log "ОШИБКА: разбор inspect $name"; rm -f "$cj" "$cj.nets"; return 1; fi



    first_net=$(sed -n '1p' "$cj.nets" | cut -f2)
    first_alias=$(sed -n '1p' "$cj.nets" | cut -f3)
    if [ -n "$first_net" ] && [ "$first_net" != "bridge" ]; then
        set -- "$@" --network "$first_net"
        if [ -n "$first_alias" ]; then
            tab=$(printf '\t'); old_ifs=$IFS; IFS=,
            for al in $first_alias; do set -- "$@" --network-alias "$al"; done
            IFS=$old_ifs
        fi
    fi



    ep=$(jq -r '(.[0].Config.Entrypoint // [])[0] // empty | gsub("[\r\n]";" ")' "$cj" 2>/dev/null)
    [ -n "$ep" ] && set -- "$@" --entrypoint "$ep"

    set -- "$@" "$image"
    while IFS= read -r a; do set -- "$@" "$a"; done <<EOF
$(jq -r '(((.[0].Config.Entrypoint // [])[1:]) + (.[0].Config.Cmd // []))[] | gsub("[\r\n]";" ")' "$cj" 2>/dev/null)
EOF
    rm -f "$cj"


    i=2; base_rb="$rb"
    while docker inspect "$rb" >/dev/null 2>&1; do rb="$base_rb-$i"; i=$((i + 1)); done

    docker rename "$name" "$rb" || { log "ОШИБКА: rename $name → $rb"; rm -f "$cj.nets"; return 1; }
    docker stop "$rb" >/dev/null 2>&1 || true

    if ! docker create "$@" >/dev/null 2>&1; then
        log "ОШИБКА: docker create $name — возвращаю старый"
        docker rename "$rb" "$name" || true
        docker start "$name" >/dev/null 2>&1 || true
        rm -f "$cj.nets"; return 1
    fi

    if [ "$(wc -l < "$cj.nets" 2>/dev/null || echo 0)" -gt 1 ]; then
        tail -n +2 "$cj.nets" | while IFS="$(printf '\t')" read -r _ net aliases; do
            [ -n "$net" ] || continue
            set --
            old_ifs=$IFS; IFS=,
            for al in $aliases; do set -- "$@" --alias "$al"; done
            IFS=$old_ifs
            docker network connect "$@" "$net" "$name" >/dev/null 2>&1 \
                || log "WARN: сеть $net не подключена"
        done
    fi
    rm -f "$cj.nets"
    if ! docker start "$name" >/dev/null 2>&1; then
        log "ОШИБКА: docker start $name — возвращаю старый"
        docker rm -f "$name" >/dev/null 2>&1 || true
        docker rename "$rb" "$name" || true
        docker start "$name" >/dev/null 2>&1 || true
        return 1
    fi
    return 0
}


rollback() {
    tmp="$1"; new="$2"; rb="$3"
    log "откат: возвращаю $rb"
    docker rm -f "$TARGET_CONTAINER" >/dev/null 2>&1 || true
    docker rename "$rb" "$TARGET_CONTAINER" >/dev/null 2>&1 || true
    if [ -d "$tmp/data" ]; then
        docker cp "$tmp/data/." "$TARGET_CONTAINER:/data/" 2>/dev/null \
            || log "WARN: данные из бэкапа не вернулись"
    fi
    docker start "$TARGET_CONTAINER" >/dev/null 2>&1 || true
    if healthcheck; then
        log "откат успешен — на прежней версии"
        write_state rolled_back "$new" ""
    else
        log "КРИТИЧНО: откат не поднялся — вмешайся вручную (бэкап /state/backups/pre-update-$new.tgz)"
        write_state rollback_failed "$new" "manual intervention"
    fi
}



self_update() {
    src="$1"
    [ -f "$src/services/updater/Dockerfile" ] || return 0
    log "self-update updater'а — сборка образа + helper-контейнер"

    docker tag "$SELF_IMAGE:new" "$SELF_IMAGE:prev" >/dev/null 2>&1 || true
    timeout 900 docker build -q -t "$SELF_IMAGE:new" "$src/services/updater" >/dev/null 2>&1 || {
        log "WARN: образ updater'а не собрался — self-update пропущен"; return 0; }
    net=$(docker inspect "$SELF_CONTAINER" 2>/dev/null \
          | jq -r '.[0].NetworkSettings.Networks | keys[0] // ""' 2>/dev/null)
    set -- -d --rm --name mg-updater-selfupdate \
        -v /var/run/docker.sock:/var/run/docker.sock
    [ -n "$net" ] && set -- "$@" --network "$net"
    set -- "$@" -e "SELF_CONTAINER=$SELF_CONTAINER" -e "SELF_IMAGE=$SELF_IMAGE" \
        --entrypoint sh "$SELF_IMAGE:new" \
        -c 'sleep 3
            rb="$SELF_CONTAINER-rollback-$(date +%s)"
            sh /updater.sh --clone "$SELF_CONTAINER" "$SELF_IMAGE:new" "$rb" || exit 1
            sleep 15
            docker inspect -f "{{.State.Running}}" "$SELF_CONTAINER" 2>/dev/null | grep -q true \
                && docker rm "$rb" >/dev/null 2>&1'
    docker run "$@" >/dev/null 2>&1 \
        && log "self-update helper запущен (updater пересоздастся сам)" \
        || log "WARN: self-update helper не запустился (updater обновится при следующем ручном compose up)"
}

_do_update() {
    new="$1"; tmp="$2"
    case "$new" in ""|*[!0-9.]*) log "ОШИБКА: мусорная версия '$new'"; return 1 ;; esac

    log "скачиваю релиз $new с $STORE_URL"
    curl -fsS --max-time 120 --max-filesize 268435456 \
        "$STORE_URL/api/releases/latest.tar.gz" -o "$tmp/rel.tar.gz" || {
        log "ОШИБКА: tarball не скачался"; write_state download_failed "$new" ""; return 1; }
    sz=$(wc -c < "$tmp/rel.tar.gz" 2>/dev/null || echo 0)
    if [ "$sz" -gt 268435456 ] || [ "$sz" -lt 1024 ]; then
        log "ОШИБКА: размер tarball $sz вне допустимого — отмена"
        write_state bad_archive "$new" "size $sz"; return 1
    fi
    curl -fsS --max-time 20 "$STORE_URL/api/releases/latest.sha256" -o "$tmp/rel.sha256" || {
        log "ОШИБКА: sha256 не скачался"; write_state download_failed "$new" "no sha256"; return 1; }
    sum_name=$(awk '{print $2}' "$tmp/rel.sha256")
    case "$sum_name" in ""|*/*|*..*)
        log "ОШИБКА: подозрительное имя в sha256"; write_state bad_archive "$new" ""; return 1 ;; esac
    cp "$tmp/rel.tar.gz" "$tmp/$sum_name"
    ( cd "$tmp" && sha256sum -c "rel.sha256" ) >/dev/null 2>&1 || {
        log "ОШИБКА: sha256 НЕ СОШЛАСЬ — обновление отменено"; write_state sha_mismatch "$new" ""; return 1; }


    curl -fsS --max-time 20 "$STORE_URL/api/releases/latest.sig" -o "$tmp/rel.sig" || {
        log "ОШИБКА: подпись не скачалась"; write_state download_failed "$new" "no sig"; return 1; }
    openssl pkeyutl -verify -pubin -inkey /release.pub.pem -rawin \
        -in "$tmp/rel.tar.gz" -sigfile "$tmp/rel.sig" >/dev/null 2>&1 || {
        log "ОШИБКА: ПОДПИСЬ НЕВАЛИДНА — обновление отклонено"; write_state sig_invalid "$new" ""; return 1; }

    archive_safe "$tmp/rel.tar.gz" || {
        log "ОШИБКА: в архиве опасные пути/типы — отменено"; write_state bad_archive "$new" ""; return 1; }

    mkdir "$tmp/src" && tar xzf "$tmp/rel.tar.gz" --no-same-permissions -C "$tmp/src" || {
        log "ОШИБКА: распаковка tarball"; write_state extract_failed "$new" ""; return 1; }


    unpacked_mb=$(du -sm "$tmp/src" 2>/dev/null | awk '{print $1}')
    case "$unpacked_mb" in ""|*[!0-9]*) unpacked_mb=9999 ;; esac
    if [ "$unpacked_mb" -gt 2048 ]; then
        log "ОШИБКА: распакованный размер ${unpacked_mb}MB > 2048MB — отмена"
        write_state bad_archive "$new" "unpacked ${unpacked_mb}MB"; return 1
    fi


    mkdir -p "$STATE_DIR/backups"
    if docker cp "$TARGET_CONTAINER:/data/." "$tmp/data" 2>/dev/null; then
        tar czf "$STATE_DIR/backups/pre-update-$new.tgz" -C "$tmp" data 2>/dev/null \
            || log "WARN: tar бэкапа не удался — продолжаю (код откатываемый)"
    else
        log "WARN: бэкап data/ не удался — продолжаю (код откатываемый)"
    fi
    ( cd "$STATE_DIR/backups" && ls -1t pre-update-*.tgz 2>/dev/null | tail -n +6 | xargs -r rm -f ) || true

    PREV_IMAGE=$(docker inspect -f '{{.Image}}' "$TARGET_CONTAINER" 2>/dev/null || echo "")
    export PREV_IMAGE

    log "сборка образа $new (контекст уходит демону потоком — пути хоста не нужны)"
    timeout 1800 docker build -q -t "$TARGET_IMAGE:$new" "$tmp/src" >/dev/null 2>&1 || {
        log "ОШИБКА: docker build (timeout 30мин или ошибка) — старый контейнер не тронут"
        write_state build_failed "$new" ""; return 1; }

    prev_ver=$(current_version)
    rb="$TARGET_CONTAINER-rollback-$(date +%s)"
    log "пересоздание $TARGET_CONTAINER (клон по inspect; старый → $rb)"
    clone_recreate "$TARGET_CONTAINER" "$TARGET_IMAGE:$new" "$rb" || {
        write_state recreate_failed "$new" ""; return 1; }

    if healthcheck && [ "$(current_version)" = "$new" ]; then
        log "OK: $new развёрнута, healthcheck зелёный"
        docker rm "$rb" >/dev/null 2>&1 || true


        docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
            | grep "^$TARGET_IMAGE:" | while read -r t; do
                case "$t" in
                    "$TARGET_IMAGE:$new"|"$TARGET_IMAGE:$prev_ver") ;;
                    *) docker rmi "$t" >/dev/null 2>&1 || true ;;
                esac
            done
        write_state updated "$new" ""
        self_update "$tmp/src"
        return 0
    fi
    log "ОШИБКА: healthcheck/сверка версии — откат"
    rollback "$tmp" "$new" "$rb"
    return 1
}

do_update() {
    tmp=$(mktemp -d) || return 1
    ( trap 'rm -rf "$tmp"' EXIT; _do_update "$1" "$tmp" )
}





take_trigger() {
    if docker exec "$TARGET_CONTAINER" test -f /data/.update-now 2>/dev/null; then
        docker exec "$TARGET_CONTAINER" rm -f /data/.update-now 2>/dev/null \
            || log "WARN: не смог погасить .update-now — будет ретриггер каждые 5с"
        return 0
    fi
    return 1
}




case "${1:-}" in
    --clone)
        shift
        clone_recreate "$1" "$2" "$3"
        exit $?
        ;;
    --take-trigger)
        take_trigger; exit $? ;;
    --version)
        echo "maxgraph-updater 0.24.1 (watchtower-style)"; exit 0 ;;
    "") ;;
    *) echo "updater: неизвестный аргумент: $1" >&2; exit 2 ;;
esac

if [ -n "${HOST_REPO_DIR:-}" ]; then
    log "HOST_REPO_DIR упразднён в v0.22.0 (watchtower-style) — игнорирую"
fi

case "$STORE_URL" in
    https://*) ;;
    *) if [ "$AUTO_UPDATE" = "1" ]; then
           log "STORE_URL не https — автообновление отключено (подпись по открытому каналу бессмысленна)"
           AUTO_UPDATE=0
       fi ;;
esac


case "$CHECK_INTERVAL" in ""|*[!0-9]*) CHECK_INTERVAL=3600 ;; esac
[ "$CHECK_INTERVAL" -lt 300 ] && CHECK_INTERVAL=300

log "старт: target=$TARGET_CONTAINER store=$STORE_URL auto=$AUTO_UPDATE интервал=${CHECK_INTERVAL}s"
[ "$AUTO_UPDATE" = "1" ] || log "AUTO_UPDATE != 1 — только проверяю и сообщаю"



if ! docker inspect "$TARGET_CONTAINER" >/dev/null 2>&1; then
    last_rb=$(docker ps -a --format '{{.Names}}' 2>/dev/null \
        | grep "^$TARGET_CONTAINER-rollback-" | sort | tail -1)
    if [ -n "$last_rb" ]; then
        log "ВОССТАНОВЛЕНИЕ: $TARGET_CONTAINER отсутствует, возвращаю $last_rb"
        docker rename "$last_rb" "$TARGET_CONTAINER" 2>/dev/null \
            && docker start "$TARGET_CONTAINER" >/dev/null 2>&1 \
            && log "восстановлен из $last_rb"
    fi
fi

sleep 45
FORCE=0
while :; do


    docker ps -a --format '{{.Names}}' 2>/dev/null \
        | grep "^$TARGET_CONTAINER-rollback-" | while read -r c; do
            docker rm -f "$c" >/dev/null 2>&1 || true
        done
    cur=$(current_version)
    latest=$(fetch_latest)
    if [ -z "$latest" ]; then
        log "магазин недоступен — следующая попытка через ${CHECK_INTERVAL}s"
    elif ver_newer "$latest" "$cur"; then
        if [ "$AUTO_UPDATE" = "1" ] || [ "$FORCE" = "1" ]; then
            do_update "$latest" || true
        else
            log "доступно обновление $latest (текущая $cur) — AUTO_UPDATE выключен (кнопка в дашборде ставит сразу)"
            write_state available "$latest" ""
        fi
    else
        log "актуально ($cur)"
    fi
    FORCE=0
    [ "$RUN_ONCE" = "1" ] && exit 0




    waited=0
    while [ "$waited" -lt "$CHECK_INTERVAL" ]; do
        if take_trigger; then
            case "$STORE_URL" in
                https://*) FORCE=1; log "триггер из дашборда — проверяю сейчас" ;;
                *) log "триггер из дашборда отклонён: STORE_URL не https" ;;
            esac
            break
        fi
        sleep 5; waited=$((waited + 5))
    done
done
