# Maxgraph — Agent Instructions

## 🚀 Ты — агент, и пользователь попросил тебя установить MaxGraph (память)

Сделай всё сам, шаг за шагом:

1. **Склонируй репозиторий**: `git clone https://github.com/RuMemory/maxgraph` и перейди в каталог проекта.
2. **Запусти сервер** — один из вариантов:
   - Docker (предпочтительно): `docker compose up -d`
   - Без Docker: нужен Python 3.12+, затем `pip install "mcp<2"` и `python app/app.py`
3. **Возьми токен доступа**: он генерируется сам при первом запуске — в файле `data/local_token.txt` (или в логах: `docker compose logs`).
4. **Подключи MCP к хосту пользователя**: эндпоинт `http://localhost:8787/mcp`, заголовок `Authorization: Bearer <токен>`. Готовые конфиги под Claude Code, Cursor и другие хосты — в `docs/PAIRING.md`.
5. **Проверь установку**: сохрани тестовый факт (`create_entities`) и прочитай его обратно (`search_nodes`). Сообщи пользователю, что память работает.
6. **Читай дальше**: `maxgraph-agent.md` (протокол памяти — вставь в system-prompt) и `docs/PROTOCOL.md`. Дашборд с живым графом: `http://localhost:8787/app`.

**EN:** A user asked you to install MaxGraph (memory). Clone the repo → run `docker compose up -d` (or Python 3.12+: `pip install "mcp<2"` then `python app/app.py`) → take the auto-generated token from `data/local_token.txt` → connect `http://localhost:8787/mcp` with `Authorization: Bearer <token>` (host configs in `docs/PAIRING.md`) → verify by saving and reading back a test fact → then read `maxgraph-agent.md` and `docs/PROTOCOL.md`. Dashboard: `http://localhost:8787/app`.

## О продукте

Память полного сохранения контекста: MCP (streamable-http, stateless) + REST + дашборд в одном Docker-контейнере.
Публичный репо: github.com/RuMemory/maxgraph. Прод-магазин: https://rezon-ai.ru.

## Правила кода

- Python 3.12, чистый stdlib в модулях (`billing.py`, `licensing.py`, `digiseller.py`, `webhooks.py`, `tgbot.py`) — каждый тестируется отдельно от сервера (инъекция http/smtp), `app.py` — единственный тяжёлый (mcp/starlette)
- HTML-страницы — однофайловые, ноль внешних URL, данные сервера только через `textContent` (см. `docs/DESIGN-SYSTEM-v3.md`)
- Сеть в хендлерах — после снятия `_codes_lock`, никогда внутри лока
- Секреты — только env (`.env` на сервере), никогда в репо; compose подставляет пустую строку → использовать `or default`, не `os.environ.get(x, default)`
- Тесты: `python -m unittest discover -s app -p "test_*.py"` (перед коммитом; 88 шт.)
- Conventional commits: `<type>: <description>`

## Agent skills

Процессы разработки (триаж issues, доменные документы, ADR) ведутся в приватном репозитории разработки; эта витрина содержит только продукт.
