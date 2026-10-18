# 🧠 Инструкции памяти Maxgraph для твоего агента

Готовый блок для копипаста в system-prompt, `AGENTS.md` или `CLAUDE.md` **любого** агента, подключённого к Maxgraph по MCP. Он учит агента вести память по протоколу: читать граф на старте, писать факты сразу, фиксировать итог сессии.

В `AGENTS.md` проекта блок вставляется в КОРНЕ рабочей папки проекта между маркерами `<!-- maxgraph:start -->` и `<!-- maxgraph:end -->`; повторная установка заменяет только содержимое между маркерами. Не в подпапку установки и не в папки хоста.

Подключение агента к серверу — [docs/PAIRING.md](docs/PAIRING.md).

## Блок (RU)

```text
У тебя есть постоянная память Maxgraph (инструменты maxgraph/*).

ПРОЕКТ = текущая рабочая папка пользователя. Вычисли её slug один раз на сессию
(kebab-case: git remote → корень git-репозитория → имя папки) и передавай
параметр project=<slug> КАЖДОМУ вызову maxgraph — так память пишется и читается
в проекте пользователя, а сам проект появляется в графе автоматически.

СТАРТ СЕССИИ: первым вызови session_start(project=<slug>) — вернётся готовый
блок контекста проекта. Нужна конкретная тема — search_nodes(query, project=<slug>).

ВО ВРЕМЯ РАБОТЫ — ПИШИ СРАЗУ, не откладывай на конец разговора:
- новый проект/сервис/файл/инструмент → create_entities (с title, entityType, tags);
  name = kebab-case id навсегда, title = человеческое имя;
  перед созданием — search_nodes, существующие не дублировать;
- новый durable-факт о существующем → add_observations
  (одно наблюдение = один атомарный факт, со ссылкой на дату/версию, если важно);
- объект X использует/зависит от Y → create_relations в активном залоге
  ("uses", "depends on", "configured by", "hosted on", "part of" — не "is used by");
- изменился статус/параметр → add_observations (новый факт с датой),
  при смене имени — update_entity;
- дело «на потом» → task_add; завершил задачу → task_done.

КОНЕЦ СЕССИИ: вызови session_close(project=<slug>, summary="...", done=[...]) —
итоги и связи запишутся сами.

ЗАХВАТ-ХУКИ (проверь один раз на этой машине): если существует
~/.maxgraph/maxgraph_hook.py — зарегистрируй его в конфиге СВОЕГО хоста (только
своего; про другие программы пользователя не спрашивай и их конфиги не трогай).
Команда: `python3 ~/.maxgraph/maxgraph_hook.py --host <хост>` (Windows: pythonw
вместо python3), события: SessionStart, UserPromptSubmit, PostToolUse, Stop,
SessionEnd (или ближайшие аналоги хоста). Куда вписать: Claude Code —
~/.claude/settings.json (ключ hooks); Cursor — ~/.cursor/hooks.json; Codex —
~/.codex/hooks.json; Kimi Code — ~/.kimi-code/config.toml ([[hooks]]); Gemini CLI —
~/.gemini/settings.json (ключ hooks); Antigravity — ~/.gemini/config/hooks.json;
Windsurf/Devin — ~/.codeium/windsurf/hooks.json. Точные сниппеты — docs/HOOKS.md
в папке установки. Команда с maxgraph_hook.py уже есть в конфиге или файла хука
нет — пропусти молча. Регистрация идемпотентна, дублей не делай.

УДАЛЕНИЕ MAXGRAPH по просьбе пользователя — полный порядок, без хвостов:
1) в папке установки запусти uninstall.sh (macOS/Linux) / uninstall.ps1 (Windows) —
   снимает контейнеры/образы и служебные остатки; данные памяти (./data) по
   умолчанию СОХРАНЯЮТСЯ, полное стирание — только после явного «да» пользователя
   или с флагом --wipe-data;
2) удали запись "maxgraph" из конфигов MCP-клиентов и регистрации захват-хуков
   (команда с maxgraph_hook.py) из конфига своего хоста — что именно удалять:
   docs/HOOKS.md, раздел «Удаление»;
3) удали этот блок (между <!-- maxgraph:start --> и <!-- maxgraph:end -->) из
   AGENTS.md; в ГЛОБАЛЬНЫХ правилах агента — покажи пользователю найденные строки
   про maxgraph и удаляй только установочные блоки после его явного «да»;
4) если данные сохранялись — ПЕРЕНЕСИ ./data из папки установки до её удаления
   (или удали всё, кроме data/): удаление папки не должно унести память.
   Папку удаляй после подтверждения пользователя.
Папка установки уже удалена? Тогда: docker rm -f maxgraph maxgraph-updater,
потом шаги 2–4 вручную.

Полный протокол: https://github.com/RuMemory/maxgraph/blob/master/docs/PROTOCOL.md
```

## Блок (EN, короткий)

```text
You have persistent memory via Maxgraph (maxgraph/* tools).

PROJECT = the user's current working folder. Derive its slug once per session
(kebab-case: git remote → git repo root → folder name) and pass project=<slug>
to EVERY maxgraph call — memory is then written and read inside the user's
project, and the project registers in the graph automatically.

Session start: call session_start(project=<slug>) FIRST — it returns a ready
context block for the project. For a specific topic: search_nodes(query, project=<slug>).
While working — WRITE IMMEDIATELY, never batch to the end:
new topic → create_entities (name = kebab-case id, title = human name; search first, don't duplicate);
new durable fact about an existing entity → add_observations (one observation = one atomic fact, dated when it matters);
dependencies → create_relations in active voice ("uses", "depends on", "configured by").
Something for later → task_add; finished a task → task_done.
Session end: call session_close(project=<slug>, summary="...", done=[...]) —
outcomes and links are recorded for you.

CAPTURE HOOKS (check once per machine): if ~/.maxgraph/maxgraph_hook.py exists —
register it in YOUR OWN host's config (your host only; never ask the user about
other programs or touch their configs). Command: `python3 ~/.maxgraph/maxgraph_hook.py
--host <host>` (on Windows: pythonw instead of python3); events: SessionStart,
UserPromptSubmit, PostToolUse, Stop, SessionEnd (or the host's closest equivalents).
Where: Claude Code — ~/.claude/settings.json (hooks key); Cursor — ~/.cursor/hooks.json;
Codex — ~/.codex/hooks.json; Kimi Code — ~/.kimi-code/config.toml ([[hooks]]);
Gemini CLI — ~/.gemini/settings.json (hooks key); Antigravity — ~/.gemini/config/hooks.json;
Windsurf/Devin — ~/.codeium/windsurf/hooks.json. Exact snippets — docs/HOOKS.md
in the install folder. If a maxgraph_hook.py command is already in the config, or
the hook file is missing — skip silently. Registration is idempotent, no duplicates.

UNINSTALL on the user's request — full cleanup, no leftovers:
1) run uninstall.sh (macOS/Linux) / uninstall.ps1 (Windows) in the install folder —
   removes containers/images and leftovers; memory data (./data) is KEPT by default,
   wipe only after the user's explicit "yes" or with --wipe-data;
2) remove the "maxgraph" entry from MCP client configs and the capture-hook
   registrations (the maxgraph_hook.py command) from your host's config — what to
   remove: docs/HOOKS.md, "Removal" section;
3) remove this block (between <!-- maxgraph:start --> and <!-- maxgraph:end -->)
   from AGENTS.md; in the agent's GLOBAL rules — show the user any maxgraph
   lines found and remove only install-made blocks after their explicit "yes";
4) if data was kept — MOVE ./data out of the install folder BEFORE deleting it
   (or delete everything except data/): removing the folder must not take the
   memory with it. Delete the folder after the user's confirmation.
Install folder already gone? Then: docker rm -f maxgraph maxgraph-updater,
then steps 2–4 by hand.

Full protocol: https://github.com/RuMemory/maxgraph/blob/master/docs/PROTOCOL.md
```
