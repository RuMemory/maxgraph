# Maxgraph — Agent Instructions

## 🚀 Ты — агент, и пользователь попросил тебя установить MaxGraph (память)

Сделай всё сам, шаг за шагом:

1. **Склонируй репозиторий**: `git clone https://github.com/RuMemory/maxgraph` и перейди в каталог проекта.
2. **Проверь Docker**: `docker info` отвечает без ошибок и `docker compose version` работает → Docker есть, установщик пойдёт коротким путём. Нет → установщик поставит движок сам (шаг 3).
3. **Запусти установщик** из корня репозитория — он проверит/поставит движок, вычислит `HOST_FP` (sha256 железа хоста — привязка триала и лицензии; существующий непустой НЕ перезаписывает; сырой идентификатор никуда не отправляется), запишет `.env` и поднимет сервер (готовый образ GHCR; при его недоступности — локальная сборка):
   - macOS/Linux: `bash install.sh --yes`
   - Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Yes`

   **Docker отсутствует → СНАЧАЛА спроси пользователя** (на его языке): «Docker на машине не найден. Установщик скачает с официального сайта docker.com и запустит установщик Docker Desktop (на Linux — официальный Docker Engine + добавление пользователя в группу docker — постоянные права, эквивалентные root): скачивание ~600 МБ; права администратора запросит сам установщик Docker; на Windows возможна одна перезагрузка — после входа установка продолжится автоматически. Ставим?» Согласие → та же команда с `--yes`/`-Yes`. Отказ → установщик предложит Docker Desktop ещё раз как рекомендуемый вариант; повторный отказ → шаг 3a. Python НИКОГДА не предлагай как равноправную опцию и не помечай «рекомендуемым»: рекомендуемый путь — всегда Docker, Python — только фолбэк после отказа(ов). Во время установки на экране появится окно установщика Docker — пользователю нужно пройти его шаги (подтвердить права администратора; вход в аккаунт Docker можно пропустить).

   Коды выхода: `0` — MaxGraph запущен, иди к шагу 4; `2` — Docker Desktop просит перезагрузку Windows (или ждёт действий в своём окне): после входа пользователя установка продолжится автоматически (предупреди об этом); если автопродолжение не сработало — пользователь завершает диалоги Docker Desktop и перезапускает `install.ps1`; `3` — движок не установлен → шаг 3a; `1` — ошибка → читай `install.log`.

   **Рассказывай пользователю прогресс по ходу** (что качается, что ставится, сколько это обычно занимает): первая установка без Docker может занять до часа на медленном интернете — это разовая цена, дальше обновления приходят сами. Молчаливый час ожидания выглядит для пользователя как зависание.

3a. **Python-фолбэк** (ТОЛЬКО если Docker отклонён или недоступен; нужен Python 3.12+):
   1. `pip install "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"` (`mcp<2` обязательно: в 2.x `FastMCP` переименован)
   2. Вычисли `HOST_FP` и запиши строкой `HOST_FP=<64 hex>` в `.env` (нет файла → создай копией `.env.example`; непустой не перезаписывай):
      - Windows (PowerShell): `$guid=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid; [BitConverter]::ToString([System.Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes("$guid"))).Replace('-','').ToLower()`
      - macOS: `printf %s "$(ioreg -rd1 -c IOPlatformExpertDevice | awk -F'"' '/IOPlatformUUID/{print $4}')" | shasum -a 256 | awk '{print $1}'`
      - Linux: `tr -d '[:space:]' < /etc/machine-id | sha256sum | awk '{print $1}'`
   3. Запуск ИЗ КОРНЯ репозитория и обязательно с `MEMORY_DIR=./data` (иначе данные уйдут в `/data` и не подхватятся при будущем переходе на Docker):
      - Windows: `$env:MEMORY_DIR="./data"; python app/app.py`
      - macOS/Linux: `MEMORY_DIR=./data python app/app.py`
   4. **Обязательно скажи пользователю**, что Python-режим теряет: автообновления (только вручную: `git pull` + `pip install -U …`), автозапуск (после перезагрузки сервер надо запускать снова), изоляцию сети (слушает 0.0.0.0 — виден в локальной сети, доступ по токену). Переход на Docker позже: поставить Docker и запустить install-скрипт из папки установки — данные `./data` подхватятся.
4. **Возьми токен доступа**: он генерируется сам при первом запуске — в файле `data/local_token.txt` (или в логах: `docker compose logs`).
5. **Подключи MCP к хосту пользователя**: эндпоинт `http://localhost:8787/mcp`, заголовок `Authorization: Bearer <токен>`. Готовые конфиги под Claude Code, Cursor и другие хосты — в `docs/PAIRING.md`.
6. **Проверь установку**: сохрани тестовый факт (`create_entities`) и прочитай его обратно (`search_nodes`). Сообщи пользователю, что память работает.
7. **Читай дальше**: `maxgraph-agent.md` (протокол памяти — установи его по шагу 8) и `docs/PROTOCOL.md`. Дашборд с живым графом: `http://localhost:8787/app`.
8. **Установи правила памяти в проект пользователя** — в КОРЕНЬ текущей рабочей папки проекта (cwd, где запущен ты, агент-установщик; папку проекта пользователь создаёт сам, в произвольном месте):
   - `./AGENTS.md` уже существует → допиши блок из `maxgraph-agent.md` между маркерами `<!-- maxgraph:start -->` и `<!-- maxgraph:end -->`. Повторная установка заменяет только содержимое между маркерами — остальное содержимое файла не трогай (идемпотентно).
   - `./AGENTS.md` нет → создай файл с этим блоком между теми же маркерами.
   - ЗАПРЕЩЕНО класть правила в подпапку установки (`maxgraph/…`) или в папки хоста (IDE/CLI) — агент проекта их там не увидит, и память работать не будет.
9. **Скажи пользователю явно**: «Перезапусти хост (IDE/CLI полностью, не крестиком в окно) и начни НОВУЮ сессию в папке проекта» — MCP-подключение и правила из `./AGENTS.md` подхватываются только при старте сессии. Без этого шага пользователь продолжит старую сессию и решит, что память не работает.

**EN:** A user asked you to install MaxGraph (memory). Clone the repo → check Docker (`docker info` and `docker compose version` both work) → run the installer from the repo root: `bash install.sh --yes` (macOS/Linux) or `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Yes` (Windows). The installer installs a missing engine itself (official Docker Desktop from docker.com on Win/mac, official Docker Engine on Linux), computes `HOST_FP` (sha256 of the host hardware id; NEVER overwrites an existing non-empty value; the raw id never leaves the host), writes `.env` and starts the server (prebuilt GHCR image, local build as fallback). **If Docker is missing, ask the user FIRST** (in their language): "Docker not found. The installer will download and run the official Docker Desktop installer from docker.com (on Linux: the official Docker Engine script — it also adds your user to the docker group, permanent root-equivalent rights): ~600 MB download; Docker's installer requests admin rights itself; on Windows one reboot may be required — installation continues automatically afterwards. Proceed?" A declined offer is repeated once with Docker Desktop as the recommended option; a second refusal → step 3a. NEVER offer Python as an equal option or mark it as recommended — Docker is always the recommended path; Python is only the fallback after refusal(s). During setup a Docker Desktop installer window appears on screen — the user clicks through it (admin prompt; sign-in can be skipped). Installer exit codes: `0` = running, continue; `2` = Docker Desktop asks for a Windows reboot or has a dialog waiting — the installer resumes automatically at logon (warn the user; if auto-resume did not fire, the user finishes the Docker Desktop dialogs and re-runs `install.ps1`); `3` = engine declined/impossible → Python fallback (step 3a: `pip install "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"`, write `HOST_FP` per the commands in step 3a, run `MEMORY_DIR=./data python app/app.py` from the repo root — and you MUST tell the user what Python mode loses: no auto-updates, no autostart after reboot, server listens on 0.0.0.0 = visible on the LAN, token required; migrate later by installing Docker and re-running the installer — `./data` is picked up); `1` = error, read `install.log`. **Narrate progress to the user as it happens** (what is downloading or installing, and the usual timings): a first install without Docker can take up to an hour on a slow connection — a one-time cost, updates then arrive by themselves; a silent hour looks like a hang. → take the auto-generated token from `data/local_token.txt` → connect `http://localhost:8787/mcp` with `Authorization: Bearer <token>` (host configs in `docs/PAIRING.md`) → verify by saving and reading back a test fact → then read `maxgraph-agent.md` and `docs/PROTOCOL.md`. Dashboard: `http://localhost:8787/app`.

**EN — install destination (mandatory):** Write the memory rules from `maxgraph-agent.md` into `AGENTS.md` in the ROOT of the user's current project directory (your cwd — where you, the installer agent, were launched; the user creates this project folder themselves, anywhere). If `./AGENTS.md` already exists, insert the rules between the markers `<!-- maxgraph:start -->` and `<!-- maxgraph:end -->`; on re-install, replace only the content between the markers and leave the rest of the file untouched (idempotent). If it doesn't exist, create the file with that marked block. NEVER put the rules into the install subfolder (`maxgraph/…`) or into host (IDE/CLI) folders — the project agent won't see them there, and memory will silently not work.

**EN — final step (mandatory):** Tell the user explicitly: "Restart the host (quit the IDE/CLI fully, not just close the window) and start a NEW session in the project folder" — the MCP connection and the `./AGENTS.md` rules are picked up only at session start. Without this the user keeps the old session and concludes memory doesn't work.

## Удаление / Uninstall

Пользователь просит удалить MaxGraph → полный порядок в `llms-install.md` (раздел «Uninstall»): запусти `uninstall.sh` / `uninstall.ps1` из папки установки (данные памяти по умолчанию СОХРАНЯЮТСЯ; стирание — только после явного согласия пользователя или с флагом `--wipe-data`), затем удали MCP-запись `maxgraph` из конфигов клиентов и блок `<!-- maxgraph:start -->…<!-- maxgraph:end -->` из AGENTS.md проекта (в глобальных правилах агента — покажи пользователю найденные строки, удаляй только установочные блоки после его «да»). Данные сохранялись → ПЕРЕНЕСИ `./data` из папки ДО её удаления. Папка уже удалена → `docker rm -f maxgraph maxgraph-updater` + остальные шаги вручную.

**EN:** The user asks to remove MaxGraph → full procedure in `llms-install.md`, "Uninstall": run `uninstall.sh` / `uninstall.ps1` from the install folder (memory data is KEPT by default; wipe only after the user's explicit consent or with `--wipe-data`), then remove the `maxgraph` MCP entry from client configs and the `<!-- maxgraph:start -->…<!-- maxgraph:end -->` block from the project AGENTS.md (in the agent's global rules — show the user the lines found, remove only install-made blocks after their "yes"). Data kept → MOVE `./data` out of the folder BEFORE deleting it. Folder already deleted → `docker rm -f maxgraph maxgraph-updater` + the remaining steps by hand.

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
