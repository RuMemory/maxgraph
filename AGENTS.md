# Maxgraph — Agent Instructions

## 🚀 Ты — агент, и пользователь попросил тебя установить MaxGraph (память)

Сделай всё сам, шаг за шагом:

1. **Склонируй репозиторий**: `git clone https://github.com/RuMemory/maxgraph` и перейди в каталог проекта.
2. **Вычисли `HOST_FP` и запиши в `.env`** — стабильный fingerprint машины (привязка триала и лицензии к железу хоста), ДО запуска сервера:
   - Windows (PowerShell):
     ```powershell
     $guid=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid
     [BitConverter]::ToString([System.Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes("$guid"))).Replace('-','').ToLower()
     ```
   - macOS: `printf %s "$(ioreg -rd1 -c IOPlatformExpertDevice | awk -F'"' '/IOPlatformUUID/{print $4}')" | shasum -a 256 | awk '{print $1}'`
   - Linux: `tr -d '[:space:]' < /etc/machine-id | sha256sum | awk '{print $1}'`

   Итог — 64 hex-символа. `.env` в каталоге установки отсутствует → создай (можно копией `.env.example`); есть → обнови строку `HOST_FP=<hash>` или допиши её. **Повторная установка НЕ перезаписывает существующий непустой `HOST_FP`.** Сырой идентификатор железа никуда не отправляется — только его sha256 остаётся в `.env`.
3. **Запусти сервер** — один из вариантов:
   - Docker (предпочтительно): `docker compose up -d`
   - Без Docker: нужен Python 3.12+, затем `pip install "mcp<2"` и `python app/app.py`
4. **Возьми токен доступа**: он генерируется сам при первом запуске — в файле `data/local_token.txt` (или в логах: `docker compose logs`).
5. **Подключи MCP к хосту пользователя**: эндпоинт `http://localhost:8787/mcp`, заголовок `Authorization: Bearer <токен>`. Готовые конфиги под Claude Code, Cursor и другие хосты — в `docs/PAIRING.md`.
6. **Проверь установку**: сохрани тестовый факт (`create_entities`) и прочитай его обратно (`search_nodes`). Сообщи пользователю, что память работает.
7. **Читай дальше**: `maxgraph-agent.md` (протокол памяти — установи его по шагу 8) и `docs/PROTOCOL.md`. Дашборд с живым графом: `http://localhost:8787/app`.
8. **Установи правила памяти в проект пользователя** — в КОРЕНЬ текущей рабочей папки проекта (cwd, где запущен ты, агент-установщик; папку проекта пользователь создаёт сам, в произвольном месте):
   - `./AGENTS.md` уже существует → допиши блок из `maxgraph-agent.md` между маркерами `<!-- maxgraph:start -->` и `<!-- maxgraph:end -->`. Повторная установка заменяет только содержимое между маркерами — остальное содержимое файла не трогай (идемпотентно).
   - `./AGENTS.md` нет → создай файл с этим блоком между теми же маркерами.
   - ЗАПРЕЩЕНО класть правила в подпапку установки (`maxgraph/…`) или в папки хоста (IDE/CLI) — агент проекта их там не увидит, и память работать не будет.
9. **Скажи пользователю явно**: «Перезапусти хост (IDE/CLI полностью, не крестиком в окно) и начни НОВУЮ сессию в папке проекта» — MCP-подключение и правила из `./AGENTS.md` подхватываются только при старте сессии. Без этого шага пользователь продолжит старую сессию и решит, что память не работает.

**EN:** A user asked you to install MaxGraph (memory). Clone the repo → compute `HOST_FP` (sha256 of the host hardware id — Windows: `MachineGuid` from `HKLM\SOFTWARE\Microsoft\Cryptography`; macOS: `IOPlatformUUID` from `ioreg`; Linux: `/etc/machine-id` of the host — exact commands in step 2 above) and write it as `HOST_FP=<64 hex>` into the install dir `.env` (create it from `.env.example` if missing; NEVER overwrite an existing non-empty `HOST_FP` on reinstall; the raw hardware id never leaves the host — only the hash) → run `docker compose up -d` (or Python 3.12+: `pip install "mcp<2"` then `python app/app.py`) → take the auto-generated token from `data/local_token.txt` → connect `http://localhost:8787/mcp` with `Authorization: Bearer <token>` (host configs in `docs/PAIRING.md`) → verify by saving and reading back a test fact → then read `maxgraph-agent.md` and `docs/PROTOCOL.md`. Dashboard: `http://localhost:8787/app`.

**EN — install destination (mandatory):** Write the memory rules from `maxgraph-agent.md` into `AGENTS.md` in the ROOT of the user's current project directory (your cwd — where you, the installer agent, were launched; the user creates this project folder themselves, anywhere). If `./AGENTS.md` already exists, insert the rules between the markers `<!-- maxgraph:start -->` and `<!-- maxgraph:end -->`; on re-install, replace only the content between the markers and leave the rest of the file untouched (idempotent). If it doesn't exist, create the file with that marked block. NEVER put the rules into the install subfolder (`maxgraph/…`) or into host (IDE/CLI) folders — the project agent won't see them there, and memory will silently not work.

**EN — final step (mandatory):** Tell the user explicitly: "Restart the host (quit the IDE/CLI fully, not just close the window) and start a NEW session in the project folder" — the MCP connection and the `./AGENTS.md` rules are picked up only at session start. Without this the user keeps the old session and concludes memory doesn't work.

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
