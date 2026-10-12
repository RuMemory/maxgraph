# Maxgraph capture hooks — автоматический захват сессий

`maxgraph_hook.py` — универсальный хук (чистый stdlib, Python 3.10+), который
превращает события хоста агента (Claude Code, DSH) в память Maxgraph:

```
хук хоста (stdin JSON) → нормализация → локальный spool ~/.maxgraph/spool.jsonl
→ POST батчами по 100 на $MG_SERVER/api/ingest (Bearer $MG_TOKEN, таймаут 3с)
```

- **Fail-open**: сеть/сервер недоступны → события молча остаются в spool и
  доставятся при следующем запуске. Скрипт **всегда** завершается кодом 0 —
  хук никогда не ломает и не блокирует хоста.
- **Идемпотентность**: content-hash (SHA256 `hook+project+ts+data`) — дедуп
  внутри spool при отправке + последние 1000 хэшей на сервере. Повторная
  доставка не создаёт дублей.
- **Spool**: ротация при 10MB (остаётся хвост ~5MB целых строк).
- **Проект**: определяется каскадом `git remote → git root → имя cwd`
  (kebab-slug); сервер принимает готовый slug.

## Настройка

1. Скопируйте скрипт:

   ```bash
   mkdir -p ~/.maxgraph && cp maxgraph_hook.py ~/.maxgraph/maxgraph_hook.py
   ```

2. Задайте env (в системе или в конфиге хоста ниже):

   ```bash
   export MG_SERVER="https://memory.example.com"   # базовый URL сервера Maxgraph
   export MG_TOKEN="<MEMORY_TOKEN сервера>"
   ```

   Без `MG_SERVER`/`MG_TOKEN` хук только пишет spool (доставка начнётся, как
   только env появятся). `MG_SPOOL` переопределяет путь spool-файла.

## Claude Code (`~/.claude/settings.json`)

```json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "*", "hooks": [
        { "type": "command", "command": "python ~/.maxgraph/maxgraph_hook.py", "timeout": 10 } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [
        { "type": "command", "command": "python ~/.maxgraph/maxgraph_hook.py", "timeout": 10 } ] }
    ],
    "PostToolUse": [
      { "matcher": "*", "hooks": [
        { "type": "command", "command": "python ~/.maxgraph/maxgraph_hook.py", "timeout": 10 } ] }
    ],
    "Stop": [
      { "matcher": "*", "hooks": [
        { "type": "command", "command": "python ~/.maxgraph/maxgraph_hook.py", "timeout": 10 } ] }
    ],
    "SessionEnd": [
      { "matcher": "*", "hooks": [
        { "type": "command", "command": "python ~/.maxgraph/maxgraph_hook.py", "timeout": 10 } ] }
    ]
  }
}
```

Windows: замените `python ~/.maxgraph/...` на
`python "C:\Users\<you>\.maxgraph\maxgraph_hook.py"`.

## DSH (`cordis.patch.yml`)

DSH подключает хуки формата Claude Code через плагин
`@deepseek-ai/dsh-hooks-claude-code`.

**Windows: wrapper на `pythonw.exe`** — иначе каждый хук будет мигать окном
консоли (cmd) и пугать пользователя; а inline `cmd /c "set …&& python …"`
ломается о квотинг внутри JSON (exit 1, хук молча не работает — проверено
на DSH desktop). `pythonw` — GUI-субсистема, консоль не создаётся вообще.
Создайте `C:/Users/<you>/.maxgraph/run_hook.pyw`:

```python
# run_hook.pyw — без-консольный запуск maxgraph_hook.py с env (Windows).
import os, runpy, sys
os.environ["MG_SERVER"] = "https://memory.example.com"
os.environ["MG_TOKEN"] = "<MEMORY_TOKEN сервера>"
sys.argv = [r"C:\Users\<you>\.maxgraph\maxgraph_hook.py"]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except Exception:
    pass
sys.exit(0)
```

(любой pythonw из python.org 3.10+ подойдёт — скрипт на чистом stdlib)

Создайте конфиг хуков (например, `C:/Users/<you>/.maxgraph/hooks.json`):

```json
{
  "hooks": {
    "SessionStart":    [ { "matcher": ".*", "hooks": [ { "type": "command", "command": "\"C:/path/to/pythonw.exe\" \"C:/Users/<you>/.maxgraph/run_hook.pyw\"", "timeout": 10 } ] } ],
    "UserPromptSubmit":[ { "matcher": ".*", "hooks": [ { "type": "command", "command": "\"C:/path/to/pythonw.exe\" \"C:/Users/<you>/.maxgraph/run_hook.pyw\"", "timeout": 10 } ] } ],
    "PostToolUse":     [ { "matcher": ".*", "hooks": [ { "type": "command", "command": "\"C:/path/to/pythonw.exe\" \"C:/Users/<you>/.maxgraph/run_hook.pyw\"", "timeout": 10 } ] } ],
    "Stop":            [ { "matcher": ".*", "hooks": [ { "type": "command", "command": "\"C:/path/to/pythonw.exe\" \"C:/Users/<you>/.maxgraph/run_hook.pyw\"", "timeout": 10 } ] } ],
    "SessionEnd":      [ { "matcher": ".*", "hooks": [ { "type": "command", "command": "\"C:/path/to/pythonw.exe\" \"C:/Users/<you>/.maxgraph/run_hook.pyw\"", "timeout": 10 } ] } ]
  }
}
```

и добавьте запись в `cordis.patch.yml` профиля
(`$DSH_HOME/profiles/<profile>/cordis.patch.yml`):

```yaml
# === Maxgraph capture hooks (fail-open, спул ~/.maxgraph/spool.jsonl) ===
# Отключение: удалить блок и перезапустить DSH.
- insert:
    - id: hooks-maxgraph
      name: "@deepseek-ai/dsh-hooks-claude-code"
      config:
        configPath: C:/Users/<you>/.maxgraph/hooks.json
        projectDir: C:/path/to/workspace
```

Проверка wrapper'а: `echo {"hook_event_name":"UserPromptSubmit","cwd":"."} | "C:/path/to/pythonw.exe" run_hook.pyw` — событие появляется в spool/на сервере (exit code у GUI-приложения не виден — смотрите spool или /api/events).

## Что попадает в память

Каждое событие → дайджест ≤200 символов в пульсе (`hook_PostToolUse` и т.д.,
`source="hook"`, с slug проекта). `SessionEnd` дополнительно
создаёт сущность `session-YYYY-MM-DD-<полный slug>` («завершена, N событий») — один
раз на проект в день (до 0.14.1 — 4-буквенный префикс slug, коллидировал). Протокол ручной записи (`docs/PROTOCOL.md`) остаётся
fallback'ом и каналом для durable-фактов, которые хуки не видят.

## Проверка

```bash
echo '{"hook_event_name":"UserPromptSubmit","prompt":"тест","cwd":"."}' | python maxgraph_hook.py
echo %errorlevel%  /  echo $?   # всегда 0
cat ~/.maxgraph/spool.jsonl     # событие в spool, если сервер не настроен
```
