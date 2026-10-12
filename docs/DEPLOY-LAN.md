# 🏡 Deploy LAN — Maxgraph в локальной сети

Maxgraph полностью офлайн-совместим: ни одного внешнего вызова в коде.

## Сценарии

| Сценарий | Как |
|---|---|
| Домашний сервер / NAS | `docker compose up -d` — готово, http://nas:8787 |
| Рабочая машина + ноутбук | Запущен на рабочей машине, ноут ходит по LAN-IP |
| Закрытая сеть (без интернета) | Собери образ на машине с интернетом → `docker save`/`docker load` → перенеси |
| Raspberry Pi | Образ multi-arch (план v0.5); python:3.12-slim есть под arm64 |

## LAN + Tailscale (рекомендуем)

Дашборд и MCP доступны с телефона/ноута из любой точки, но не торчат в интернет:

```bash
tailscale up   # на сервере
# дашборд: http://<tailscale-ip>:8787/app (ввести MEMORY_TOKEN в форму входа)
```

## HTTPS

Для LAN обычно не нужен. Если нужен — nginx/Caddy перед контейнером:

```nginx
server {
  listen 443 ssl;
  server_name memory.home.lan;
  location / { proxy_pass http://127.0.0.1:8787; }
}
```

## Бэкапы в LAN

cron на сервере:
```bash
0 4 * * * curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8787/api/export \
  -o /backups/maxgraph-$(date +\%F).json && find /backups -mtime +30 -delete
```
