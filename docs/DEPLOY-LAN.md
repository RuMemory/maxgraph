# 🏡 Deploy LAN — Maxgraph в локальной сети

Память работает локально и не зависит от сети: хранение, поиск и MCP/REST — полностью на вашей машине. К магазину (rezon-ai.ru) инстанс обращается только для учёта установки, проверки лицензии и автообновлений; без интернета всё остальное работает как обычно.

## Сценарии

| Сценарий | Как |
|---|---|
| Домашний сервер / NAS | Нативная программа: `bash install.sh --yes` из клона репо — готово, http://nas:8787 |
| Рабочая машина + ноутбук | Запущен на рабочей машине, ноут ходит по LAN-IP |
| Закрытая сеть (без интернета) | Python-фолбэк из клона: `pip install "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"` → `MEMORY_DIR=./data python app/app.py` (без автообновлений и автозапуска) |
| Raspberry Pi (aarch64/armv7) | Python-фолбэк из клона (как «закрытая сеть» выше). Нативного бинарника под ARM нет: native-build.yml собирает только windows-amd64, macos-arm64 и linux-amd64, поэтому install.sh на ARM остановится с «No linux-arm64 artifact in the store manifest» |

> Legacy: установки на Docker (docker-compose.yml в корне репо) продолжают
> работать и обновляться — новым установкам Docker не предлагается.

## LAN + Tailscale (рекомендуем)

Дашборд и MCP доступны с телефона/ноута из любой точки, но не торчат в интернет:

```bash
tailscale up   # на сервере
# дашборд: http://<tailscale-ip>:8787/app (ввести MEMORY_TOKEN в форму входа)
```

## HTTPS

Для LAN обычно не нужен. Если нужен — nginx/Caddy перед сервером:

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
