# 🪝 HOOKS.md — рецепты захват-хуков для каждого агента

Хук `~/.maxgraph/maxgraph_hook.py` (кладёт установщик) превращает события хоста
(старт/конец сессии, промпт, вызов инструмента) в память MaxGraph. Адрес сервера
и токен хук берёт из `~/.maxgraph/hooks.env` — в конфигах агентов токен не нужен.

**Автоматика главная:** установщик (install.sh / install.ps1) сам регистрирует хук
во все найденные хосты (`hooks/register_hook.py --auto`, идемпотентно, с бэкапом
конфига). Этот документ — фолбэк и справочник: нестандартный хост, ручная правка,
удаление. Снятие регистраций делает uninstall (или вручную:
`python3 hooks/register_hook.py --auto --remove`).

**Правило одной строки: агент регистрирует хук только в СВОЁМ хосте** — том, в
котором он запущен. Про другие программы пользователя не спрашиваем и их конфиги
не трогаем. Регистрация идемпотентна: если команда с `maxgraph_hook.py` уже есть
в конфиге — ничего не делаем.

Команда везде одна (флаг `--host` переводит имена событий хоста в наши):

- macOS/Linux: `python3 ~/.maxgraph/maxgraph_hook.py --host <хост>`
- Windows: `pythonw "C:\Users\<you>\.maxgraph\maxgraph_hook.py" --host <хост>`
  (pythonw — без мигания консоли)

После регистрации: полностью перезапусти хост. Проверка:
`echo '{"hook_event_name":"SessionStart","cwd":"."}' | python3 ~/.maxgraph/maxgraph_hook.py --host <хост>`
— событие появится в дашборде (Хроника → пульс).

---

## Claude Code — `~/.claude/settings.json` (Windows: `%USERPROFILE%\.claude\settings.json`)

В ключ `"hooks"` добавить (существующие записи не трогать):

```json
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host claude", "timeout": 10 }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host claude", "timeout": 10 }] }],
    "PostToolUse":      [{ "matcher": "*", "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host claude", "timeout": 10 }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host claude", "timeout": 10 }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host claude", "timeout": 10 }] }]
  }
}
```

Покрытие: все 5 событий.

## DeepSeek Harness (DSH) — `cordis.patch.yml` профиля

⚠️ **Плагин `@deepseek-ai/dsh-hooks-claude-code` НЕ входит в дистрибутив DSH**
(в манифесте приложения он devDependency; проверено на 0.2.1-rc.2: файлов пакета
в app.asar нет). Ряд в `cordis.patch.yml`, ссылающийся на неустановленный пакет,
DSH молча пропускает (failed import необязательного ряда — «warn and continue»),
плагин не появляется в GUI (Plugins) и хук никогда не вызывается. Поэтому
регистрация в DSH = **два шага**: (1) установить пакет в профиль, (2) вписать ряд.
Установщик (register_hook.py) делает оба сам pnpm'ом из рантайма DSH:

```bash
# что делает установщик (вручную — то же самое, для каждого профиля):
cd ~/.dsh/profiles/<профиль>
pnpm add --save-exact --config.auto-install-peers=false --ignore-scripts \
  @deepseek-ai/dsh-hooks-claude-code@<версия DSH> \
  @deepseek-ai/dsh-hook-protocol@<версия DSH> \
  @deepseek-ai/dsh-llm@<версия DSH>
```

Самого моста мало (боевой кейс 2026-10-19): он статически импортирует
`dsh-hook-protocol` и `dsh-llm` — без них ряд молча мёртв даже при
установленном мосте. Поэтому после установки установщик прогоняет
`import()` моста бандлёным node DSH и доустанавливает всё, что не
резолвится (например, транзитивный `@deepseek-ai/cordis` — версия берётся
из package.json импортёра, у cordis своя линия 4.x, отличная от версии DSH).

- Версия пакетов = версия рантайма DSH (`~/.dsh/dsh-runtimes/*/runtime.json` →
  `desktopVersion`): peerDependencies пакетов пинят `@deepseek-ai/dsh-*` ровно этой
  сборки, иначе compat-проверка DSH откажет и ряд станет `disabled`.
  **После обновления DSH перезапусти установку хуков** — пин устареет.
- `--config.auto-install-peers=false` обязателен: лавина ВСЕХ пиров в профиле
  зашадоуит пакеты дистрибутива; ставим точечно только недостающее.
- Свой pnpm не нужен: установщик берёт node+pnpm из `~/.dsh/dsh-runtimes/*`.
  Если установка не удалась (offline и т.п.), вывод регистрации содержит готовую
  ручную команду — захват без пакетов работать не будет.

Файл `~/.maxgraph/hooks.json` (Claude-формат событий, команда с `--host dsh`):

```json
{
  "hooks": {
    "SessionStart":     [{ "matcher": ".*", "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host dsh", "timeout": 10 }] }],
    "UserPromptSubmit": [{ "matcher": ".*", "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host dsh", "timeout": 10 }] }],
    "PostToolUse":      [{ "matcher": ".*", "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host dsh", "timeout": 10 }] }],
    "Stop":             [{ "matcher": ".*", "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host dsh", "timeout": 10 }] }],
    "SessionEnd":       [{ "matcher": ".*", "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host dsh", "timeout": 10 }] }]
  }
}
```

и запись в `cordis.patch.yml`:

```yaml
- insert:
    - id: hooks-maxgraph
      name: "@deepseek-ai/dsh-hooks-claude-code"
      config:
        configPath: <абсолютный путь к ~/.maxgraph/hooks.json>
        projectDir: <рабочая папка>
```

Проверка, что DSH видит плагин: после полного перезапуска отправь любой промпт —
событие появится в дашборде (Хроника → пульс). Пусто — смотри статус
`hooks plugin …` в выводе register_hook.py: «NOT installed / install failed»
значит пакет не доехал, выполни ручную команду из статуса.

На Windows команда — через консольный `python.exe` с полным путём и оператором
вызова `&` (DSH исполняет hook-команды через PowerShell; pythonw/pythonw-подобный
GUI-питон НЕЛЬЗЯ: PowerShell не дожидается GUI-процесса, и Job-раннер DSH убивает
его до выполнения — захват молча пуст, аудит 2026-10-20). Покрытие: все 5.

## Cursor — `~/.cursor/hooks.json` (Windows: `%USERPROFILE%\.cursor\hooks.json`)

Отдельный файл, горячая перезагрузка (перезапуск не нужен). Создать/дополнить:

```json
{
  "version": 1,
  "hooks": {
    "sessionStart":       [{ "command": "python3 ~/.maxgraph/maxgraph_hook.py --host cursor" }],
    "beforeSubmitPrompt": [{ "command": "python3 ~/.maxgraph/maxgraph_hook.py --host cursor" }],
    "postToolUse":        [{ "command": "python3 ~/.maxgraph/maxgraph_hook.py --host cursor" }],
    "stop":               [{ "command": "python3 ~/.maxgraph/maxgraph_hook.py --host cursor" }],
    "sessionEnd":         [{ "command": "python3 ~/.maxgraph/maxgraph_hook.py --host cursor" }]
  }
}
```

Покрытие: все 5 событий (имена Cursor переводит `--host cursor`).

## Codex CLI — `~/.codex/hooks.json` (Windows: `%USERPROFILE%\.codex\hooks.json`)

```json
{
  "hooks": {
    "SessionStart":     [{ "matcher": "startup", "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host codex", "timeout": 10 }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host codex", "timeout": 10 }] }],
    "PostToolUse":      [{ "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host codex", "timeout": 10 }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host codex", "timeout": 10 }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host codex", "timeout": 10 }] }]
  }
}
```

⚠️ **Trust-гейт Codex**: новые хуки молча пропускаются, пока пользователь один раз
не доверит их: в Codex выполни `/hooks` и подтверди доверие к хукам maxgraph.
Скажи пользователю об этом шаге явно — без него захват не работает.

Покрытие: все 5 событий (после доверия).

## Kimi Code CLI — `~/.kimi-code/config.toml` (Windows: `%USERPROFILE%\.kimi-code\config.toml`)

Дописать в конец (существующие записи `[[hooks]]` не трогать):

```toml
[[hooks]]
event = "SessionStart"
command = "python3 ~/.maxgraph/maxgraph_hook.py --host kimi"

[[hooks]]
event = "UserPromptSubmit"
command = "python3 ~/.maxgraph/maxgraph_hook.py --host kimi"

[[hooks]]
event = "PostToolUse"
command = "python3 ~/.maxgraph/maxgraph_hook.py --host kimi"

[[hooks]]
event = "Stop"
command = "python3 ~/.maxgraph/maxgraph_hook.py --host kimi"

[[hooks]]
event = "SessionEnd"
command = "python3 ~/.maxgraph/maxgraph_hook.py --host kimi"
```

Покрытие: все 5 событий.

## Antigravity (Google) — `~/.gemini/config/hooks.json` (Windows: `%USERPROFILE%\.gemini\config\hooks.json`)

Формат — карта «имя хука → события»:

```json
{
  "maxgraph": {
    "SessionStart": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host antigravity" }],
    "PostToolUse":  [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host antigravity", "timeout": 10 }],
    "Stop":         [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host antigravity" }]
  }
}
```

⚠️ **Урезанный режим**: у Antigravity нет событий `SessionEnd` и `UserPromptSubmit`
(2026-10) — сессия дня автоматически не закрывается, промпт не захватывается;
итоги сессии пишет сам агент (`session_close`). `PreInvocation` не используем:
стреляет на каждый вызов модели — слишком шумно.

## Gemini CLI (legacy) — `~/.gemini/settings.json` (Windows: `%USERPROFILE%\.gemini\settings.json`)

В ключ `"hooks"` добавить (Gemini CLI заменяется на Antigravity — новым ставь
рецепт выше):

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host gemini", "timeout": 10 }] }],
    "BeforeAgent":  [{ "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host gemini", "timeout": 10 }] }],
    "AfterTool":    [{ "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host gemini", "timeout": 10 }] }],
    "AfterAgent":   [{ "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host gemini", "timeout": 10 }] }],
    "SessionEnd":   [{ "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host gemini", "timeout": 10 }] }]
  }
}
```

Покрытие: все 5 через аналоги (`BeforeAgent`→промпт, `AfterTool`→инструмент,
`AfterAgent`→stop).

## Windsurf / Devin Desktop — `~/.codeium/windsurf/hooks.json` (Windows: `%USERPROFILE%\.codeium\windsurf\hooks.json`)

```json
{
  "hooks": {
    "pre_user_prompt":       [{ "command": "python3 ~/.maxgraph/maxgraph_hook.py --host windsurf" }],
    "post_cascade_response": [{ "command": "python3 ~/.maxgraph/maxgraph_hook.py --host windsurf" }]
  }
}
```

⚠️ **Урезанный режим**: у Windsurf/Devin нет событий начала/конца сессии —
захватываются только промпт и ответ; итоги сессии пишет сам агент (`session_close`).

## Хосты без хуков

Cline (только TypeScript-плагины, не конфиг), Continue.dev, ChatGPT (облако) —
конфиг-хуков нет. Память работает через MCP-инструменты и дисциплину агента
(session_start/session_close) — это нормальный режим.

---

## Удаление / Removal

Агент удаляет из конфига СВОЕГО хоста записи, чьи команды содержат
`maxgraph_hook.py` (в Claude/Gemini — элементы внутри `"hooks"`; в Cursor/Windsurf/
Antigravity/Codex — из hooks.json; в Kimi — блоки `[[hooks]]` с этой командой; в
DSH — запись `hooks-maxgraph` из cordis.patch.yml и файл `~/.maxgraph/hooks.json`;
пакеты `@deepseek-ai/dsh-hooks-claude-code`, `@deepseek-ai/dsh-hook-protocol`,
`@deepseek-ai/dsh-llm`, `@deepseek-ai/cordis` из профиля DSH (`pnpm remove` в папке
профиля) удаляй только если их не используют другие ряды hooks-моста — например,
чужой `hooks-ecc` на том же пакете).
Папку `~/.maxgraph` удаляет uninstall-скрипт.

## Детали транспорта (справочно)

Хук читает JSON события из stdin, копит в `~/.maxgraph/spool.jsonl` и отправляет
батчами на `$MG_SERVER/api/ingest` (Bearer `$MG_TOKEN` из `~/.maxgraph/hooks.env`).
Fail-open: сеть/сервер недоступны — события остаются в spool и доедут позже;
скрипт всегда завершается кодом 0 и никогда не блокирует хоста. Переопределения:
`MG_SPOOL` (путь spool), `MG_HOOKS_ENV` (путь hooks.env), `MG_HOST` (вместо `--host`).

### Обратное направление (v0.49.0): память приходит в контекст сама

Помимо захвата, хук теперь отдаёт память хосту (stdout, fail-open, таймаут 3с):

- **SessionStart** → `GET /api/session-context?project=<slug>` → блок памяти
  проекта попадает в контекст сессии автоматически. Выключатель: `MG_SESSION_INJECT=0`.
- **UserPromptSubmit** → `POST /api/recall` → топ-факты проекта по тексту
  промпта (скип промптов <12 символов и начинающихся с `/ ! #`; дедуп по
  sha256 промпта в `~/.maxgraph/recalled.json`, последние 500). Факты в контексте
  помечены маркером `▣` — если факт повлиял на ответ, источник называй «из maxgraph».
  Выключатель: `MG_RECALL=0`.
- **PreToolUse** → read-only инструменты maxgraph (`search_nodes`, `open_nodes`,
  `read_graph`, `session_start`, `task_list`) разрешаются автоматически, без
  диалога подтверждения. Записи (`create_entities`, `add_observations`, `delete_*`,
  `task_add` и т.д.) всегда спрашивают пользователя. Выключатель: `MG_PREAPPROVE=0`.

Для PreToolUse в Claude Code добавь в `~/.claude/settings.json` (вместе с остальными):

```json
"PreToolUse": [{ "matcher": "mcp__.*maxgraph.*", "hooks": [{ "type": "command", "command": "python3 ~/.maxgraph/maxgraph_hook.py --host claude", "timeout": 5 }] }]
```

(в Windows-команде — pythonw с полным путём, как в шапке). Установщик регистрирует
PreToolUse сам (register_hook.py).

Переустановка со сменой токена: `hooks.env` нарочно не затирается установщиком —
при новом токене удали `~/.maxgraph/hooks.env` и перезапусти установщик (или
поправь `MG_TOKEN` в файле вручную), иначе захват молча копит spool.

---

_EN summary: one hook (`~/.maxgraph/maxgraph_hook.py --host <host>`), one command
per host above; server URL and token live only in `~/.maxgraph/hooks.env`. Register
into YOUR OWN host's config only, idempotently (skip if a `maxgraph_hook.py`
command is already there). Codex needs a one-time `/hooks` trust approval.
Antigravity and Windsurf run in a reduced mode (no session events) — the agent
closes sessions via `session_close` itself. Cline/Continue/ChatGPT-cloud have no
config hooks — MCP tools only. Removal: delete config entries whose command
contains `maxgraph_hook.py`; the uninstaller removes `~/.maxgraph`._
