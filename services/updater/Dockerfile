# maxgraph-updater (v0.24.1): sidecar автообновлений, кросс-ОС (watchtower-style).
# Базовый образ pinned по digest (docker:27-cli). compose-плагин убран в v0.22.0:
# compose больше не вызывается (образ собирается docker build из tarball'а,
# контейнер пересоздаётся clone-recreate по inspect) — минус supply-chain поверхность.
FROM docker:27-cli@sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c

# jq — разбор docker inspect при clone-recreate. Пакеты не пинned по версии:
# alpine-репо rolling, пин сломается при ротации репо; целостность — apk-подписи
# + pinned digest базы. Digest базы поднимать периодически (CVE в curl/openssl).
RUN apk add --no-cache curl openssl jq

COPY updater.sh /updater.sh
# Публичный ключ проверки подписи релизов (ed25519) — trust anchor, вшит в образ.
COPY release.pub.pem /release.pub.pem
RUN chmod +x /updater.sh && chmod 444 /release.pub.pem

# root нужен для docker socket; state — на named volume /state (update_state.json,
# бэкапы). Репо хоста больше НЕ монтируется (кросс-ОС: Docker Desktop Windows/macOS).
# Entrypoint ОДНИМ элементом: clone-recreate берёт entrypoint[0] + cmd — два
# элемента («sh» + «/updater.sh») превращались в голый sh → crash-loop (HIGH-1).
ENTRYPOINT ["/updater.sh"]
