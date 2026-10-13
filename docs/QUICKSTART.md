# 🚀 Quickstart — от находки на GitHub до живой памяти

Ты нашёл Maxgraph на GitHub. Вот твой путь (≈5 минут):

## 1. Установка

Нужен только Docker:

```bash
git clone https://github.com/RuMemory/maxgraph
cd maxgraph
docker compose up -d --build
```

Токен доступа генерируется сам при первом запуске (v0.14) — возьми его из логов или файла:

```bash
docker compose logs maxgraph | grep "автогенерация"
cat data/local_token.txt
```

Свой токен (опционально) — `MEMORY_TOKEN` в `.env` (см. `.env.example`; раньше был обязателен).

Проверка: `curl http://localhost:8787/healthz` → `ok`

### Без Docker (Python)

Если Docker недоступен — сервер запускается чистым Python 3.12 (те же зависимости, что в образе):

```bash
git clone https://github.com/RuMemory/maxgraph
cd maxgraph
pip install "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"
```

> ⚠️ **`mcp<2.0` — не случайно**: в mcp 2.x `FastMCP` переименован, импорт в `app/app.py` сломается. Ставь строго 1.x.

Запуск из корня репо. `MEMORY_TOKEN` — опционален с v0.14 (без него всё приватное отвечает 401, но сервер сам сгенерирует токен в `data/local_token.txt`); `PORT` и каталог данных `MEMORY_DIR` — по желанию (дефолты: 8787 и `/data`, на хосте лучше `./data`). Однострочно:

```powershell
# PowerShell
$env:MEMORY_TOKEN="<твой токен>"; $env:PORT="8787"; $env:MEMORY_DIR="./data"; python app/app.py
```

```bash
# bash / zsh
export MEMORY_TOKEN="<твой токен>" PORT="8787" MEMORY_DIR="./data"; python app/app.py
```

Проверка — та же: `curl http://localhost:8787/healthz` → `ok`.

> 💡 Docker-путь (§1) остаётся **рекомендуемым**: изоляция, данные в volume, healthcheck образа. Python-запуск — для разработки и окружений без Docker.

## 2. Открой дашборд

`http://localhost:8787/app` — введи `MEMORY_TOKEN` в форму входа.

Токен сохранится в браузере. Ты увидишь пустой космос — это твой будущий граф знаний.

## 3. Подключи своего агента (MCP)

Пример для Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "maxgraph": {
      "url": "http://localhost:8787/mcp",
      "headers": { "Authorization": "Bearer <MEMORY_TOKEN>" }
    }
  }
}
```

Перезапусти клиент — у агента появились инструменты `create_entities`, `add_observations`, `search_nodes`, `read_graph` и др.

## 4. Научи агента протоколом

Вставь в system-prompt / инструкции агента:

```
У тебя есть постоянная память Maxgraph (инструменты maxgraph/*).
В начале сессии вызови read_graph. Сохранять факты СРАЗУ через
add_observations; новые темы — create_entities (name = kebab-case id,
title = человеческое имя); зависимости — create_relations в активном залоге.
Полный протокол: https://github.com/RuMemory/maxgraph/blob/master/docs/PROTOCOL.md
```

## 5. Смотри, как память оживает

Каждые 8 секунд дашборд обновляется: новые сущности вспыхивают на графе.
Вкладка «Таймлайн» — всё, что агент запомнил. «Сессии» — итоги разговоров.

## 6. Бэкапы

Кнопка **⬇ export** в дашборде или `curl -H "Authorization: Bearer $T" http://localhost:8787/api/export > backup.json`.

Восстановление: останови контейнер, распакуй `memory_ndjson` в `data/memory.json`, `events_jsonl` в `data/events.jsonl`, запусти. С v0.34.0 (шарды `data/projects/`): если каталог `data/projects/` уже существует, удали его перед восстановлением — миграция разложит монолит по шардам при старте (дедуп связей при загрузке подстраховывает, но чистый старт надёжнее).

## Что дальше

- Несколько провайдеров → [PROVIDERS.md](PROVIDERS.md)
- Домашний сервер / LAN / офлайн → [DEPLOY-LAN.md](DEPLOY-LAN.md)
- Pro: активация ключа и подключение агента → [PAIRING.md](PAIRING.md)
