# MaxGraph — установка (инструкция для ИИ-агента)

Пользователь попросил установить MaxGraph — локальную долговременную память для вашей совместной работы. Делай всё сам, линейно: этот файл самодостаточен, другие документы для установки читать не нужно.

## 1. Клонируй и запусти установщик

```bash
git clone https://github.com/RuMemory/maxgraph.git && cd maxgraph
```

Запусти установщик из корня клона. `--project-dir` — корень рабочей папки пользователя (твой исходный cwd до клонирования, НЕ папка клона): туда установщик вставит блок правил памяти в `AGENTS.md` (между маркерами, идемпотентно).

- macOS / Linux: `bash install.sh --yes --project-dir "<cwd>"`
- Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Yes -ProjectDir "<cwd>"`

Установщик сам: проверит Docker (нет — с согласия пользователя поставит официальный движок с docker.com), вычислит отпечаток хоста (с машины уходят только хеши), поднимет сервер, вставит правила в проект и в конце напечатает **пакет следующих шагов**: токен, готовый MCP-конфиг и машиночитаемую строку `MAXGRAPH_NEXT_STEPS={...}`.

**Docker не найден → сначала спроси пользователя** (на его языке): «Docker на машине не найден. Установщик скачает официальный установщик Docker с docker.com (~600 МБ; права администратора запросит сам установщик Docker; на Windows возможна одна перезагрузка — после входа установка продолжится автоматически). Ставим?» Согласие → та же команда. Отказ → спроси ещё раз: Docker — рекомендуемый путь; Python никогда не предлагай как равноправный и не помечай «рекомендуемым». Повторный отказ → шаг 1a.

Рассказывай пользователю прогресс по ходу: первая установка движка может занять до часа на медленном интернете — молчаливый час выглядит как зависание.

Коды выхода: `0` — сервер запущен → шаг 2. `2` — Docker Desktop ждёт перезагрузку Windows или свой диалог: после входа установка продолжится автоматически (предупреди об этом); если нет — пользователь завершает диалоги Docker Desktop и повторно запускает install.ps1. `3` — Python-фолбэк → шаг 1a. `1` — ошибка → читай `install.log`.

### 1a. Python-фолбэк (только когда Docker отклонён или невозможен; нужен Python 3.12+)

```bash
pip install "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"   # mcp<2 обязательно: в 2.x FastMCP переименован
```

Запуск из корня клона и обязательно с `MEMORY_DIR=./data` (иначе данные уйдут в `/data` и не подхватятся при переходе на Docker):

- Windows: `$env:MEMORY_DIR="./data"; python app/app.py`
- macOS/Linux: `MEMORY_DIR=./data python app/app.py`

Честно скажи пользователю, что теряется без Docker: автообновления, автозапуск после перезагрузки, изоляция сети (сервер слушает 0.0.0.0 — виден в локальной сети, доступ по токену). Переход на Docker позже: поставить Docker и перезапустить установщик — `./data` подхватится. Правила в проект уже вставлены установщиком; токен — `data/local_token.txt` после первого запуска → шаг 2.

## 2. Подключи MCP к хосту пользователя

Эндпоинт `http://localhost:8787/mcp`, заголовок `Authorization: Bearer <токен>` (токен — в пакете установщика / строке `MAXGRAPH_NEXT_STEPS` / файле `data/local_token.txt`).

- DeepSeek Harness (DSH): установщик уже вписал MCP-ряд и хуки в `~/.dsh/profiles/*/cordis.patch.yml` (и чинит битые ряды при повторном запуске) — ничего не копируй руками. Проверка: инструменты `mcp__maxgraph__*` появляются после полного рестарта DSH.
- Claude Code: `claude plugin marketplace add RuMemory/maxgraph && claude plugin install maxgraph@maxgraph`
- Любой хост с JSON-конфигом MCP:

```json
{"mcpServers": {"maxgraph": {"type": "http", "url": "http://localhost:8787/mcp", "headers": {"Authorization": "Bearer <токен>"}}}}
```

Другие хосты (deeplinks Cursor/VS Code и т.п.) — `docs/PAIRING.md`, опционально.

## 3. Проверь захват-хуки

Установщик уже зарегистрировал хуки во всех найденных на машине хостах (строки «hooks: …» в его выводе). Если твоего хоста там нет — впиши хук в конфиг СВОЕГО хоста по рецепту из `docs/HOOKS.md` (один сниппет, идемпотентно; про другие программы пользователя не спрашивай и их конфиги не трогай). Codex: скажи пользователю один раз выполнить `/hooks` и доверить хуки — без их trust-гейта захват молча не работает. Токен в конфиги не копируй — хук сам читает `~/.maxgraph/hooks.env`.

## 4. Проверь память

1. `create_entities`: name `install-check`, один факт «MaxGraph установлен <дата>».
2. `search_nodes("install-check")` — сущность вернулась → память работает. Скажи это пользователю.

## 5. Скажи пользователю явно

«Перезапусти хост полностью (не крестиком в окно) и начни НОВУЮ сессию в папке проекта» — MCP-подключение и правила из `AGENTS.md` подхватываются только на старте сессии. Без этого пользователь продолжит старую сессию и решит, что память не работает.

Дашборд с живым графом: ссылка из пакета установщика (уже с токеном — открывается сразу, вставлять ничего не нужно).

## Удаление (по просьбе пользователя)

1. В папке установки: `bash uninstall.sh` / `powershell -File uninstall.ps1` — снимает контейнеры/образы и служебные остатки; данные памяти (`./data`) по умолчанию СОХРАНЯЮТСЯ, полное стирание — только после явного «да» пользователя или с `--wipe-data`.
2. Удали запись `maxgraph` из MCP-конфигов клиентов и регистрации захват-хуков (команда с `maxgraph_hook.py`) из конфига своего хоста — что именно удалять: `docs/HOOKS.md`, раздел «Удаление».
3. Удали блок между `<!-- maxgraph:start -->` и `<!-- maxgraph:end -->` из `AGENTS.md` проекта.
4. Если данные сохранялись — перенеси `./data` из папки установки ДО её удаления; папку удаляй после подтверждения пользователя.

---

# EN — MaxGraph installation (instructions for the AI agent)

The user asked you to install MaxGraph — local persistent memory for your work together. Do everything yourself, top to bottom: this file is self-contained, no other documents are needed for installation.

## 1. Clone and run the installer

```bash
git clone https://github.com/RuMemory/maxgraph.git && cd maxgraph
```

Run the installer from the clone root. `--project-dir` is the root of the USER's working folder (your original cwd before cloning, NOT the clone itself): the installer inserts the memory-rules block into its `AGENTS.md` (marker-delimited, idempotent).

- macOS / Linux: `bash install.sh --yes --project-dir "<cwd>"`
- Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Yes -ProjectDir "<cwd>"`

The installer itself: checks Docker (if missing — installs the official engine from docker.com, with the user's consent via you), computes the host fingerprint (hashes only leave the machine), starts the server, injects the rules into the project, and prints a **next-steps package**: token, ready MCP config, and a machine-readable line `MAXGRAPH_NEXT_STEPS={...}`.

**Docker missing → ask the user FIRST** (in their language): "Docker not found. The installer will download the official Docker installer from docker.com (~600 MB; Docker's installer requests admin rights itself; on Windows one reboot may be required — the installation continues automatically afterwards). Proceed?" Consent → same command. Refusal → ask once more: Docker is the recommended path; NEVER offer Python as an equal or recommended option. Second refusal → step 1a.

Narrate progress to the user: the first engine install can take up to an hour on slow internet — a silent hour looks like a hang.

Exit codes: `0` — server is running → step 2. `2` — Docker Desktop awaits a Windows reboot or its own dialog: the installer resumes automatically at logon (warn the user); otherwise the user finishes the Docker Desktop dialogs and re-runs install.ps1. `3` — Python fallback → step 1a. `1` — error → read `install.log`.

### 1a. Python fallback (only when Docker is declined or impossible; Python 3.12+ required)

```bash
pip install "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"   # mcp<2 is mandatory: FastMCP was renamed in 2.x
```

Run from the clone root, always with `MEMORY_DIR=./data` (otherwise data lands in `/data` and won't carry over to a later Docker switch):

- Windows: `$env:MEMORY_DIR="./data"; python app/app.py`
- macOS/Linux: `MEMORY_DIR=./data python app/app.py`

Tell the user honestly what is lost without Docker: auto-updates, autostart after reboot, network isolation (the server listens on 0.0.0.0 — visible in the LAN, token required). Later: install Docker and re-run the installer — `./data` is picked up. The rules are already injected by the installer; the token is `data/local_token.txt` after the first launch → step 2.

## 2. Connect MCP to the user's host

Endpoint `http://localhost:8787/mcp`, header `Authorization: Bearer <token>` (token — in the installer package / the `MAXGRAPH_NEXT_STEPS` line / `data/local_token.txt`).

- DeepSeek Harness (DSH): the installer already wrote the MCP row and hooks into `~/.dsh/profiles/*/cordis.patch.yml` (and repairs broken rows on re-run) — do not hand-copy anything. Check: `mcp__maxgraph__*` tools appear after a full DSH restart.
- Claude Code: `claude plugin marketplace add RuMemory/maxgraph && claude plugin install maxgraph@maxgraph`
- Any host with a JSON MCP config:

```json
{"mcpServers": {"maxgraph": {"type": "http", "url": "http://localhost:8787/mcp", "headers": {"Authorization": "Bearer <token>"}}}}
```

Other hosts (Cursor/VS Code deeplinks etc.) — `docs/PAIRING.md`, optional.

## 3. Check the capture hooks

The installer already registered the hooks into every detected host (see the "hooks: …" lines in its output). If your host is missing — register the hook in YOUR OWN host's config using the recipe in `docs/HOOKS.md` (one snippet, idempotent; never ask the user about other programs or touch their configs). Codex: tell the user to run `/hooks` once and trust the hooks — without their trust gate, capture silently does nothing. Do NOT copy the token into configs — the hook reads `~/.maxgraph/hooks.env` itself.

## 4. Verify memory

1. `create_entities`: name `install-check`, one fact "MaxGraph installed <date>".
2. `search_nodes("install-check")` — the entity comes back → memory works. Tell the user.

## 5. Tell the user explicitly

"Restart the host fully (quit the IDE/CLI, not just close the window) and start a NEW session in the project folder" — the MCP connection and the `AGENTS.md` rules are picked up only at session start. Without this the user keeps the old session and concludes memory doesn't work.

Dashboard with the live graph: the link from the installer package (already carries the token — opens signed in, nothing to paste).

## Uninstall (on the user's request)

1. In the install folder: `bash uninstall.sh` / `powershell -File uninstall.ps1` — removes containers/images and service leftovers; memory data (`./data`) is KEPT by default, full wipe only after the user's explicit "yes" or with `--wipe-data`.
2. Remove the `maxgraph` entry from the clients' MCP configs and the capture-hook registrations (the `maxgraph_hook.py` command) from your host's config — what to remove: `docs/HOOKS.md`, "Removal".
3. Remove the block between `<!-- maxgraph:start -->` and `<!-- maxgraph:end -->` from the project's `AGENTS.md`.
4. If data was kept — move `./data` out of the install folder BEFORE deleting it; delete the folder after the user's confirmation.

---

Разработка продукта (правила кода, процессы, ADR) — `DEV.md`, только в dev-репозитории; агенту-установщику не нужно.
Product development (code rules, processes, ADRs) — `DEV.md`, dev repo only; not needed for installation.
