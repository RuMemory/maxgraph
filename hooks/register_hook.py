#!/usr/bin/env python3
"""register_hook.py — автоматическая (де)регистрация захват-хука MaxGraph
в конфигах хостов агентов (v0.48.1). Чистый stdlib, Python 3.10+.

Установщик вызывает:  python3 hooks/register_hook.py --auto
  --auto     — все хосты: где конфиг найден → регистрируем, где нет → пропускаем
               молча (про другие программы пользователя вопросов не задаём)
  --host X   — один хост (claude/dsh/cursor/codex/kimi/gemini/antigravity/windsurf)
  --remove   — снять регистрацию (uninstall)
  --dry-run  — только показать, что бы сделали

DSH дополнительно (v0.48.1): в профильные cordis.patch.yml вписывается и
MCP-ряд (dsh-mcp-client, streamable-http) — только в форме `- insert:` (голый
`- id:` DSH молча пропускает, см. docs/PROVIDERS.md); битый голый ряд
mcp-maxgraph/mcp-remotemem от старой доки переписывается в правильную форму.
Токен — из data/local_token.txt рядом с репо (или env MEMORY_TOKEN).

DSH, пакет плагина-моста: @deepseek-ai/dsh-hooks-claude-code НЕ входит в
дистрибутив DSH — ряд на неустановленный пакет молча мёртв (warn-and-continue),
поэтому пакет доустанавливается в профиль pnpm'ом из рантайма DSH с пином к
desktopVersion. Мало поставить сам мост (слои 2-3 бага 2026-10-19): он
статически импортирует @deepseek-ai/dsh-hook-protocol и @deepseek-ai/dsh-llm
(та же сборка — пин к desktopVersion), а те транзитивно тянут
@deepseek-ai/cordis (своя линия версий 4.x — spec выводится из package.json
импортёра). Финальный арбитр полноты — import()-прогон моста бандлёным node:
что не резолвится, доустанавливается циклом (см. _dsh_ensure_hook_plugin).

Свойства: идемпотентно (команда с maxgraph_hook.py уже в конфиге → пропуск),
перед первой записью конфига — бэкап <файл>.bak-maxgraph (один раз, не затираем),
запись атомарная (tmp + os.replace) с сохранением прав; конфиг-симлинк пишется
в цель (realpath), сам линк не заменяется (security-ревизия M2); битый JSON или
неожиданная структура конфига пользователя — пропуск, ничего не ломаем.
Всегда exit 0, кроме неизвестного хоста (exit 2) — установщик не должен падать.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MARK = "maxgraph_hook.py"   # метка наших записей во всех форматах
TIMEOUT = 10


def _home(p: str) -> Path:
    return Path(p).expanduser()


def _ps_str(s: str) -> str:
    """Экранирование для двойных кавычек PowerShell: backtick и $ иначе
    раскрываются как интерполяция (легальные символы в Windows-путях)."""
    return s.replace("`", "``").replace("$", "`$").replace('"', '""')


def _command(host: str) -> str:
    """Команда вызова хука. Windows: полный путь — в JSON-конфигах ~ и
    %USERPROFILE% не везде разворачиваются.
    DSH исполняет hook-команды через PowerShell: `"py" "hook"` — ParserError
    (UnexpectedToken), хук умирал до запуска (аудит 2026-10-20). Для dsh —
    оператор вызова `&`.
    DSH+Windows: ТОЛЬКО консольный python.exe, НЕ pythonw — PowerShell не ждёт
    GUI-процесс (pwsh выходит за ~180мс с exit 0, Job-раннер DSH убивает
    pythonw до выполнения; события терялись, stdout с additionalContext не
    доходил — аудит 2026-10-20). Остальные хосты (cmd/sh) pythonw переваривают."""
    hook = _home("~/.maxgraph/maxgraph_hook.py")
    if os.name == "nt":
        if host == "dsh":
            con = Path(sys.executable)
            if con.name.lower() == "pythonw.exe":
                cand = con.with_name("python.exe")
                con = cand if cand.exists() else con  # python.exe рядом не всегда есть
            return f'& "{_ps_str(str(con))}" "{_ps_str(str(hook))}" --host {host}'
        pyw = Path(sys.executable).with_name("pythonw.exe")
        py = str(pyw if pyw.exists() else Path(sys.executable))
        return f'"{py}" "{hook}" --host {host}'
    return f"python3 ~/.maxgraph/maxgraph_hook.py --host {host}"


def _toml_str(s: str) -> str:
    """TOML-строка: literal ('…') по умолчанию — пути Windows с кавычками и
    бэкслешами не ломают файл (security-ревизия H1); basic-строка с
    экранированием — только если внутри одинарная кавычка/перевод строки."""
    if "'" not in s and "\n" not in s:
        return "'" + s + "'"
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yaml_str(s: str) -> str:
    """YAML double-quoted скаляр; пути уже с forward slashes. Бэкслеш
    экранируется ПЕРВЫМ (security-ревизия F1: иначе бэкслеш перед закрывающей
    кавычкой экранирует её саму и ломает весь файл)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Допустимый алфавит токена для YAML-ряда (F1): наш mg_local_<hex> и типовые
# bearer-строки; всё прочее — не пишем в чужой конфиг, сообщаем skip.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]+$")


# --- формы записей per host ---------------------------------------------------

def _entry_settings(host: str, event: str) -> dict:
    """Claude/Codex/Gemini: {"hooks":[{"type":"command",...}]} (+matcher)."""
    e = {"hooks": [{"type": "command", "command": _command(host), "timeout": TIMEOUT}]}
    if host == "claude" and event == "PostToolUse":
        e["matcher"] = "*"
    if host == "claude" and event == "PreToolUse":
        # v0.49.0: только maxgraph-инструменты — auto-approve read-only в хуке;
        # прочие PreToolUse хук молча пропускает.
        e["matcher"] = "mcp__.*maxgraph.*"
        e["hooks"][0]["timeout"] = 5
    if host == "codex" and event == "SessionStart":
        e["matcher"] = "startup"
    return e


def _entry_flat(host: str, _event: str) -> dict:
    """Cursor/Windsurf: {"command": ...}."""
    return {"command": _command(host)}


def _entry_named(host: str, _event: str) -> list:
    """Antigravity: событие → список {"type":"command",...} под ключом "maxgraph"."""
    return [{"type": "command", "command": _command(host), "timeout": TIMEOUT}]


HOSTS = {
    "claude": {
        "detect": ["~/.claude"], "config": "~/.claude/settings.json",
        "kind": "settings", "entry": _entry_settings,
        "events": ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop",
                   "SessionEnd", "PreToolUse"],
    },
    "gemini": {
        "detect": ["~/.gemini/settings.json"], "config": "~/.gemini/settings.json",
        "kind": "settings", "entry": _entry_settings,
        "events": ["SessionStart", "BeforeAgent", "AfterTool", "AfterAgent", "SessionEnd"],
    },
    "codex": {
        "detect": ["~/.codex"], "config": "~/.codex/hooks.json",
        "kind": "settings", "entry": _entry_settings,
        "events": ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"],
        "note": "codex-trust",   # после записи нужен одноразовый /hooks → доверить
    },
    "cursor": {
        "detect": ["~/.cursor"], "config": "~/.cursor/hooks.json",
        "kind": "flat", "entry": _entry_flat,
        "events": ["sessionStart", "beforeSubmitPrompt", "postToolUse", "stop", "sessionEnd"],
        "root": {"version": 1},
    },
    "windsurf": {
        "detect": ["~/.codeium/windsurf"], "config": "~/.codeium/windsurf/hooks.json",
        "kind": "flat", "entry": _entry_flat,
        "events": ["pre_user_prompt", "post_cascade_response"],
    },
    "antigravity": {
        "detect": ["~/.gemini/config", "~/.gemini/antigravity-cli", "~/.gemini/antigravity"],
        "config": "~/.gemini/config/hooks.json",
        "kind": "named", "entry": _entry_named,
        "events": ["SessionStart", "PostToolUse", "Stop"],
    },
    "kimi": {
        "detect": ["~/.kimi-code"], "config": "~/.kimi-code/config.toml",
        "kind": "toml",
        "events": ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"],
    },
    "dsh": {
        "detect": ["~/.dsh/profiles"], "config": None,   # многофайловый (см. ниже)
        "kind": "dsh",
        "events": ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"],
    },
}


# --- файловые утилиты (security-ревизия M2: симлинк-конфиги и права) -----------

def _real(path: Path) -> Path:
    """Симлинк-конфиг (dotfiles-схемы): пишем в ЦЕЛЬ, сам линк не заменяем."""
    return Path(os.path.realpath(path))


def _read_cfg(path: Path) -> str:
    """utf-8-sig: BOM от PowerShell-редакторов на Windows ломал всё молча —
    regex'ы структуры (`^\\s*-?\\s*id:` не перешагнут \\ufeff), json.loads падает,
    top-level-проверка DSH отказывала в ремонте (боевой кейс покупателя
    2026-10-18: файл с BOM → «skip (top level is not a list)», ряд не чинился).
    Пишем всегда без BOM (utf-8) — все хосты его читают."""
    return path.read_text(encoding="utf-8-sig")


def _load_json(path: Path):
    """dict или None: файла нет → {} (создадим), битый/не-dict → None (не трогаем)."""
    path = _real(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(_read_cfg(path))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _backup_once(path: Path) -> None:
    path = _real(path)
    bak = path.with_name(path.name + ".bak-maxgraph")
    if path.exists() and not bak.exists():
        try:
            bak.write_bytes(path.read_bytes())
            shutil.copymode(path, bak)   # F3: бэкап с секретами — не шире прав оригинала
        except OSError:
            pass


def _atomic_write(path: Path, text: str) -> None:
    """tmp + os.replace в цель симлинка; права исходного сохраняем (в конфигах
    бывают API-ключи с 0600 — не размягчаем до umask). Имя tmp уникально по pid
    (F4: два конкурентных прогона не пишут в один файл), fsync до replace."""
    path = _real(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_once(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    try:
        if path.exists():
            shutil.copymode(path, tmp)
    except OSError:
        pass
    os.replace(tmp, path)


def _write_json(path: Path, data: dict, dry: bool) -> None:
    if dry:
        return
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _marked(text: str) -> bool:
    """Маркер в содержимом, игнорируя строки-комментарии (L4: упоминание
    maxgraph_hook.py в комментарии ≠ наша запись)."""
    return any(MARK in ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _restore_text(path: Path, text: str) -> None:
    """Откат к только что прочитанному содержимому (R2-Low: НЕ из старого
    .bak-maxgraph — он со времени первой записи мог устареть). Тот же
    атомарный путь, что и запись (F4: write_text напрямую не атомарен)."""
    try:
        _atomic_write(path, text)
    except OSError:
        pass


# --- установка / удаление per kind ---------------------------------------------

def _hooks_map(data: dict):
    """hooks-ключ конфига: нет → создаём; есть, но не map → None (не трогаем,
    security-ревизия L2)."""
    h = data.get("hooks")
    if h is None:
        h = {}
        data["hooks"] = h
        return h
    return h if isinstance(h, dict) else None


def _apply_settings(cfg_path: Path, spec: dict, remove: bool, dry: bool) -> str:
    data = _load_json(cfg_path)
    if data is None:
        return "skip (config is not valid JSON — left untouched)"
    hooks = _hooks_map(data)
    if hooks is None:
        return "skip ('hooks' key is not a map — left untouched)"
    changed = False
    for ev in spec["events"]:
        lst = hooks.setdefault(ev, [])
        if not isinstance(lst, list):
            continue   # чужое содержимое события не трогаем
        if remove:
            new = [x for x in lst if MARK not in json.dumps(x, ensure_ascii=False)]
            if len(new) != len(lst):
                changed = True
            if new:
                hooks[ev] = new
            else:
                hooks.pop(ev, None)
        else:
            if any(MARK in json.dumps(x, ensure_ascii=False) for x in lst):
                continue
            lst.append(spec["entry"](spec["_host"], ev))
            changed = True
    if not changed:
        return "already registered" if not remove else "not registered"
    if remove and not hooks:
        data.pop("hooks", None)
    _write_json(cfg_path, data, dry)
    return ("removed from " if remove else "registered in ") + str(cfg_path)


def _apply_flat(cfg_path: Path, spec: dict, remove: bool, dry: bool) -> str:
    data = _load_json(cfg_path)
    if data is None:
        return "skip (config is not valid JSON — left untouched)"
    for k, v in spec.get("root", {}).items():   # cursor: {"version": 1}
        data.setdefault(k, v)
    hooks = _hooks_map(data)
    if hooks is None:
        return "skip ('hooks' key is not a map — left untouched)"
    changed = False
    for ev in spec["events"]:
        lst = hooks.setdefault(ev, [])
        if not isinstance(lst, list):
            continue
        if remove:
            new = [x for x in lst
                   if not (isinstance(x, dict) and MARK in str(x.get("command", "")))]
            changed = changed or len(new) != len(lst)
            if new:
                hooks[ev] = new
            else:
                hooks.pop(ev, None)
        else:
            if any(isinstance(x, dict) and MARK in str(x.get("command", "")) for x in lst):
                continue
            lst.append(spec["entry"](spec["_host"], ev))
            changed = True
    if not changed:
        return "already registered" if not remove else "not registered"
    if remove and not hooks:
        data.pop("hooks", None)
    _write_json(cfg_path, data, dry)
    return ("removed from " if remove else "registered in ") + str(cfg_path)


def _apply_named(cfg_path: Path, spec: dict, remove: bool, dry: bool) -> str:
    data = _load_json(cfg_path)
    if data is None:
        return "skip (config is not valid JSON — left untouched)"
    if remove:
        # L3: сносим ключ "maxgraph" только если он действительно наш (MARK внутри)
        ours = data.get("maxgraph")
        if not ours or MARK not in json.dumps(ours, ensure_ascii=False):
            return "not registered"
        data.pop("maxgraph", None)
        _write_json(cfg_path, data, dry)
        return "removed from " + str(cfg_path)
    if "maxgraph" in data:
        return "already registered"
    data["maxgraph"] = {ev: spec["entry"](spec["_host"], ev) for ev in spec["events"]}
    _write_json(cfg_path, data, dry)
    return "registered in " + str(cfg_path)


def _toml_blocks(text: str) -> list:
    """Разрез по ЛЮБОЙ таблице/блоку (M1: и [[…]], и […] — иначе чужая секция
    после нашего блока удалялась вместе с ним при --remove)."""
    lines = text.splitlines(keepends=True)
    blocks, cur = [], []
    for ln in lines:
        if ln.lstrip().startswith("[") and cur:
            blocks.append("".join(cur))
            cur = []
        cur.append(ln)
    if cur:
        blocks.append("".join(cur))
    return blocks


def _apply_toml(cfg_path: Path, spec: dict, remove: bool, dry: bool) -> str:
    cfg_path = _real(cfg_path)
    text = _read_cfg(cfg_path) if cfg_path.exists() else ""
    if remove:
        if not _marked(text):
            return "not registered"
        kept = [b for b in _toml_blocks(text)
                if not (b.lstrip().startswith("[[hooks]]") and _marked(b))]
        out = "".join(kept)
        if not dry:
            _atomic_write(cfg_path, out)
        return "removed from " + str(cfg_path)
    if _marked(text):
        return "already registered"
    add = "".join(f'\n[[hooks]]\nevent = "{ev}"\ncommand = {_toml_str(_command(spec["_host"]))}\n'
                  for ev in spec["events"])
    if not dry:
        _atomic_write(cfg_path, text.rstrip("\n") + "\n" + add)
    return "registered in " + str(cfg_path)


_DSH_BLOCK = """- insert:
    - id: hooks-maxgraph
      name: "@deepseek-ai/dsh-hooks-claude-code"
      config:
        configPath: {config_path}
        projectDir: {project_dir}
"""

_DSH_MCP_BLOCK = """- insert:
    - id: mcp-maxgraph
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: maxgraph
        transport: streamable-http
        url: {url}
        headers:
          Authorization: {auth}
"""

# Наши MCP-ряды: id из актуальной доки и из старой (remotemem-эпохи PAIRING.md).
# Матчим ТОЧНЫЕ значения (F2: «maxgraph-dev»/«mcp-maxgraph-backup» — не наши),
# только в некомментарных строках.
_MCP_ID_RE = re.compile(r"^\s*-?\s*id:\s*[\"']?(mcp-maxgraph|mcp-remotemem)[\"']?\s*(#.*)?$",
                        re.M)
_MCP_SERVER_RE = re.compile(r"^\s*serverName:\s*[\"']?(maxgraph|remotemem)[\"']?\s*(#.*)?$",
                            re.M)
_HOOKS_ID_RE = re.compile(r"^\s*-?\s*id:\s*[\"']?hooks-maxgraph[\"']?\s*(#.*)?$", re.M)
_BLOCK_SCALAR_RE = re.compile(r":\s*[|>][+-]?\d?\s*(#.*)?$")


def _no_comments(text: str) -> str:
    """Текст без строк-комментариев (F2/L4: маркер в комментарии ≠ наша запись)."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _strip_block_scalars(lines: list) -> list:
    """Убрать СОДЕРЖИМОЕ блочных скаляров (key: | / >) — там текст, а не
    структура (L-b: маркеры внутри note: | чужого ряда не делают его нашим)."""
    out = []
    scalar_indent = None
    for ln in lines:
        stripped = ln.strip()
        ind = len(ln) - len(ln.lstrip(" "))
        if scalar_indent is not None:
            if not stripped or ind > scalar_indent:
                continue            # содержимое скаляра
            scalar_indent = None
        out.append(ln)
        if _BLOCK_SCALAR_RE.search(ln):
            scalar_indent = ind
    return out


def _is_our_mcp_row(lines: list) -> bool:
    """Наш ряд = пакет dsh-mcp-client И (наш id ИЛИ наш serverName), маркеры —
    в структурных строках (не в скалярах/комментариях). Без пакета не считаем:
    чужой ряд, упоминающий наши маркеры, трогать нельзя (F2)."""
    t = _no_comments("\n".join(_strip_block_scalars(lines)))
    return "dsh-mcp-client" in t and (bool(_MCP_ID_RE.search(t))
                                    or bool(_MCP_SERVER_RE.search(t)))


def _is_our_hooks_row(lines: list) -> bool:
    """L-a: точный id hooks-maxgraph (hooks-maxgraph-backup — не наш)."""
    return bool(_HOOKS_ID_RE.search(_no_comments("\n".join(_strip_block_scalars(lines)))))


# --- DSH: пакет плагина-моста --------------------------------------------------
# Пакет НЕ входит в дистрибутив DSH (в манифесте приложения он devDependency,
# файлов пакета в app.asar нет — проверено на 0.2.1-rc.2). Ряд `- insert:` на
# неустановленный пакет молча умирает: необязательный entry с failed import —
# «warn and continue» (dsh-app-boot), в GUI (Plugins) такой ряд не показывается.
# Поэтому одной записи в cordis.patch.yml мало: пакет надо поставить в профиль
# (резолвер подхватывает <профиль>/node_modules — installedProfilePackageNames /
# routeScoped в dsh-app-boot). Версия пинится точно к версии рантайма:
# peerDependencies пакета — ровно @deepseek-ai/dsh-* той же сборки.
_DSH_HOOKS_PKG = "@deepseek-ai/dsh-hooks-claude-code"

# Пиры моста из той же сборки DSH, которые он импортирует статически: без них
# ряд hooks-maxgraph молча мёртв даже при установленном мосте (боевой кейс
# 2026-10-19). Пин — к desktopVersion, как у моста. cordis сюда НЕ входит: у
# него своя линия версий (4.x при desktopVersion 0.2.x), spec выводится из
# package.json импортёра в _dsh_peer_spec.
_DSH_HOOKS_PEERS = ("@deepseek-ai/dsh-hook-protocol", "@deepseek-ai/dsh-llm")

# Имя пакета из вывода node (граница доверия: строка попадает в argv pnpm) —
# только легальный npm-name; флагоподобные строки ('--config=…') отбрасываются.
_DSH_PKG_NAME_RE = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
# range из package.json импортёра: точная версия или ~/^-диапазон, без пробелов
# (workspace:*, >=, 'x || y' не принимаем — такой пакет ставится руками).
_DSH_RANGE_RE = re.compile(r"^[~^]?[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")

# Сколько недостающих пакетов добиваем циклом «import → поставить» за прогон
# (потолок против бесконечного цикла при систематически падающем import).
_DSH_REPAIR_LIMIT = 8


# допустимый алфавит версии из runtime.json (граница доверия: файл пользователя
# попадает в команду pnpm — только semver-символы, никаких пробелов и '-flag')
# security-ревизия 2026-10-19 (L2): строго semver-ядро [+пререлиз] — npm
# dist-tags (latest/beta) не пропускаем, unpinned install запрещён.
_DSH_VER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")

# security-ревизия 2026-10-19 (M1): рантайм берём ТОЛЬКО из каталога
# dsh-primary-runtime — это каталог, из которого DSH физически исполняется:
# доверять ему не добавляет новой поверхности (кто может его подменить, тот
# уже исполняет код от имени пользователя). Сканировать соседние каталоги и
# выбирать max-версию НЕЛЬЗЯ: фейковый рантайм с версией "9999.0.0" выигрывал
# бы отбор и давал code-exec при benign-запуске установщика.
_DSH_PRIMARY_RUNTIME = "~/.dsh/dsh-runtimes/dsh-primary-runtime"


def _dsh_runtime() -> tuple:
    """(desktopVersion, node, pnpm.cjs) первичного рантайма DSH с бинарями.
    Версия и pnpm — из ОДНОГО рантайма: пин совпадает с фактическим движком
    (security-ревизия I3). ("", "", "") — рантайм не найден/битый."""
    rt = _home(_DSH_PRIMARY_RUNTIME) / "runtime.json"
    try:
        ver = json.loads(_read_cfg(rt)).get("desktopVersion") or ""
    except Exception:
        return ("", "", "")
    if not (isinstance(ver, str) and _DSH_VER_RE.fullmatch(ver)):
        return ("", "", "")
    base = rt.parent / "dependencies"
    node = base / "node" / "bin" / ("node.exe" if os.name == "nt" else "node")
    pnpm = base / "pnpm" / "bin" / "pnpm.cjs"
    if not (node.exists() and pnpm.exists()):
        return ("", "", "")
    return (ver, str(node), str(pnpm))


def _dsh_pkg_version(profile_dir: Path, pkg: str) -> str:
    """Версия установленного пакета профиля; "" — не установлен/битый.
    security-ревизия 2026-10-19 (M2): одного существования package.json мало —
    после обновления DSH пин обязан ПЕРЕустановиться на новую desktopVersion."""
    pj = profile_dir / "node_modules" / pkg / "package.json"
    try:
        v = json.loads(_read_cfg(pj)).get("version")
        return v if isinstance(v, str) else ""
    except Exception:
        return ""


def _sanitize_err(line: str) -> str:
    """security-ревизия 2026-10-19 (L1): хвост stderr pnpm может содержать
    registry URL с basic-auth кредами из .npmrc — режем userinfo, строку
    ограничиваем; в статус/транскрипт уходит только безопасный хвост."""
    return re.sub(r"://[^@/\s]+@", "://", line)[:200]


def _dsh_bridge_entry(profile_dir: Path) -> Path:
    """Точка входа моста для import-проверки."""
    return profile_dir / "node_modules" / _DSH_HOOKS_PKG / "lib" / "index.js"


_EMPTY_NPMRC = None   # путь к пустому npmrc, создаётся лениво один раз


def _empty_npmrc() -> str:
    """Пустой npmrc для --config.userconfig: отрезает ~/.npmrc — scoped-registry
    (@deepseek-ai:registry=…) из него обходил бы принудительный --registry
    (security-ревизия 2026-10-24, MED-1: сторож ставит unattended). Файл наш,
    постоянный (~/.maxgraph/empty.npmrc), перезаписывается пустым при
    расхождении — не mkstemp: сторож перезагружает модуль каждый прогон,
    mkstemp копил бы мусор в %TEMP% (r2 LOW-6). pnpm v11: --userconfig как
    опция add НЕВАЛИДЕН (unknown option → exit 1, ревизия r2 HIGH-1) —
    только config-dot форма; --globalconfig в v11 отсутствует вовсе."""
    global _EMPTY_NPMRC
    if _EMPTY_NPMRC is None:
        p = _home("~/.maxgraph/empty.npmrc")
        try:
            if p.is_symlink():
                raise OSError("symlink")   # r3 LOW-1: не затирать чужой ~/.npmrc
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists() or p.read_text(encoding="utf-8", errors="replace") != "":
                p.write_text("", encoding="ascii")
        except OSError:
            fd, path = tempfile.mkstemp(prefix="mg-empty-", suffix=".npmrc")
            os.close(fd)
            p = Path(path)
        _EMPTY_NPMRC = str(p)
    return _EMPTY_NPMRC


def _child_env() -> dict:
    """env для дочерних процессов (pnpm/node) БЕЗ MEMORY_TOKEN: connect_hosts
    ставит токен в os.environ на время регистрации, а сторож может запустить
    pnpm в этом окне — токен не должен наследоваться (2026-10-24, LOW-4)."""
    return {k: v for k, v in os.environ.items() if k != "MEMORY_TOKEN"}


def _dsh_pnpm_add(node: str, pnpm: str, profile_dir: Path, specs: list) -> tuple:
    """Один вызов pnpm add для списка спек. (ok, хвост ошибки — уже
    санитизированный, для статуса). returncode не доверяем: вызыватель сверяет
    фактические версии/импорт после установки (security-ревизия M3)."""
    try:
        empty = _empty_npmrc()
        r = subprocess.run([node, pnpm, "add", "--save-exact",
                            "--config.auto-install-peers=false",
                            "--ignore-scripts",
                            "--registry=https://registry.npmjs.org",
                            "--config.@deepseek-ai:registry=https://registry.npmjs.org",
                            f"--config.userconfig={empty}",
                            *specs],
                           cwd=str(profile_dir), capture_output=True, timeout=300,
                           env=_child_env())
    except Exception as ex:
        return (False, f" {type(ex).__name__}")
    if r.returncode == 0:
        return (True, "")
    err = r.stderr.decode("utf-8", "replace").strip().splitlines()
    return (False, f" exit {r.returncode}"
                   + (": " + _sanitize_err(err[-1]) if err else ""))


def _dsh_import_check(node: str, entry: Path) -> tuple:
    """(ok, missing_pkg, detail). Реальный import() моста бандлёным node — та же
    резолюция, что у загрузчика DSH: ловит недостающие пиры, которых проверка
    версий не видит (2026-10-19: мост и hook-protocol стояли, dsh-llm/cordis —
    нет, хуки молчали). missing_pkg — имя из 'Cannot find package', detail —
    путь импортёра (для вывода range) либо санитизированный хвост ошибки.
    INFO-1 (security-ревизия): import() исполняет top-level код пакета — это
    приемлемо: пакет ставится из официального registry с --ignore-scripts и
    пином к desktopVersion, а тот же top-level код DSH исполнил бы при загрузке
    плагина (проверка не добавляет поверхности, а лишь отвечает раньше)."""
    if not entry.exists():
        return (False, "", "нет " + str(entry))
    script = ("import(" + json.dumps(entry.resolve().as_uri()) + ")"
              ".then(()=>console.log('IMPORT OK'))"
              ".catch(e=>{console.error('IMPORT FAIL: '+e.message);process.exit(3)})")
    try:
        r = subprocess.run([node, "--input-type=module", "-e", script],
                           capture_output=True, timeout=60, env=_child_env())
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
    except Exception as ex:
        return (False, "", type(ex).__name__)
    if "IMPORT OK" in out:
        return (True, "", "")
    m = re.search(r"Cannot find package '([^']+)' imported from (.+)", out)
    if m:
        # путь импортёра — до конца строки (LOW-1: Windows-пути с пробелами
        # 'C:\Users\John Doe\…' — \S+ обрезал их, легитимный кейс уходил в ручной)
        return (False, m.group(1), m.group(2).rstrip("\r").strip())
    tail = out.strip().splitlines()
    return (False, "", _sanitize_err(tail[-1]) if tail else f"exit {r.returncode}")


def _dsh_peer_spec(profile_dir: Path, name: str, importer: str, ver: str) -> str:
    """spec '<pkg>@<версия>' для недостающего пакета из import-проверки.
    @deepseek-ai/dsh-* — пин к desktopVersion (lockstep-сборка); прочие
    (cordis и т.п.) — range из package.json импортёра (peerDependencies →
    dependencies → devDependencies). "" — spec построить нельзя (нелегальное
    имя, импортёр вне node_modules профиля, нет пригодного range): ставится
    руками, автоматике такое не доверяем."""
    if not _DSH_PKG_NAME_RE.fullmatch(name):
        return ""
    if name.startswith("@deepseek-ai/dsh-"):
        return f"{name}@{ver}"
    try:
        root = (profile_dir / "node_modules").resolve()
        d = Path(importer).resolve().parent
        d.relative_to(root)   # импортёр обязан жить в node_modules профиля
    except (ValueError, OSError):
        return ""
    while True:
        pj = d / "package.json"
        if pj.exists():
            try:
                meta = json.loads(_read_cfg(pj))
            except Exception:
                return ""
            for key in ("peerDependencies", "dependencies", "devDependencies"):
                rng = (meta.get(key) or {}).get(name)
                if isinstance(rng, str) and _DSH_RANGE_RE.fullmatch(rng):
                    return f"{name}@{rng}"
            return ""
        if d == root:
            return ""
        parent = d.parent
        if parent == d:
            return ""
        d = parent


def _dsh_ensure_hook_plugin(profile_dir: Path, dry: bool) -> str:
    """Доустановка моста и его пиров в профиль + import-проверка замыкания.
    --config.auto-install-peers=false обязателен: лавина ВСЕХ пиров зашадоуила
    бы пакеты дистрибутива (routeScoped: кандидат из профиля выигрывает у
    entries) — ставим точечно только то, чего реально не хватает для import.
    Без известной версии рантайма НЕ ставим unpinned latest (security-ревизия
    I2: peer-mismatch всё равно отключит ряд + supply-chain окно) — только
    ручная команда. Ошибка установки не роняет регистрацию.
    security-ревизия 2026-10-19: M2 — сверка версий с desktopVersion
    (переустановка при mismatch); M3 — registry принудительно официальный,
    после установки сверяем version из package.json с запрошенной (имя пакета
    пинится самим путём в node_modules, отдельная проверка name не нужна).
    2026-10-19 (слои 2-3): замыкание = мост + _DSH_HOOKS_PEERS; дальше —
    import-цикл: недостающий пакет из вывода node → spec (dsh-* пином к
    desktopVersion, прочие range'ом импортёра) → доустановка, максимум
    2026-10-19 (слои 2-3): замыкание = мост + _DSH_HOOKS_PEERS; дальше —
    import-цикл: недостающий пакет из вывода node → spec (dsh-* пином к
    desktopVersion, прочие range'ом импортёра) → доустановка, максимум
    _DSH_REPAIR_LIMIT итераций.
    2026-10-24 (ревизия сторожа): LOW-1 — после установки перечитываем
    runtime.json: версия сменилась в полёте → пин повторяем под новую
    (до 3 раундов). LOW-2 (свежий runtime.json = апдейт в полёте) живёт
    в dsh_ensure_all_profiles — только unattended-контур; ручная установка
    пользователя откладываться не должна."""
    ver, node, pnpm = _dsh_runtime()
    pkgs = (_DSH_HOOKS_PKG, *_DSH_HOOKS_PEERS)

    def _manual(v: str) -> str:
        specs = [f"{p}@{v}" if v else f"{p}@<версия DSH>" for p in pkgs]
        return (f'cd "{profile_dir}" && pnpm add --save-exact '
                f"--config.auto-install-peers=false --ignore-scripts "
                f"--registry=https://registry.npmjs.org {' '.join(specs)}")

    if dry:
        specs = [f"{p}@{ver}" if ver else f"{p}@<версия DSH>" for p in pkgs]
        return f"would install {' '.join(specs)} into {profile_dir}"
    if not node or not ver:
        return (f"hooks plugin NOT installed (DSH не вызовет хук) — полностью "
                f"перезапустите DSH (runtime появится) и повторите: "
                f"python hooks/register_hook.py --host dsh; либо вручную: "
                f"{_manual(ver)}")
    was_present = any(_dsh_pkg_version(profile_dir, p) for p in pkgs)
    did_pin = False      # менялись основные пины (мост+пиры)
    did_repair = False   # добивались недостающие пакеты из import-цикла
    for _pin_round in range(3):   # LOW-1: +раунд, если версия сменилась в полёте
        stale = [p for p in pkgs if _dsh_pkg_version(profile_dir, p) != ver]
        if not stale:
            break
        ok, err = _dsh_pnpm_add(node, pnpm, profile_dir, [f"{p}@{ver}" for p in stale])
        bad = [p for p in stale if _dsh_pkg_version(profile_dir, p) != ver]
        if not ok or bad:
            if not ok:
                why = f" (pnpm{err})"
            else:
                got = ", ".join(_dsh_pkg_version(profile_dir, p) or "нет" for p in stale)
                why = f" (установлено {got} ≠ {ver})"
            return (f"hooks plugin install failed{why} — установите вручную: "
                    f"{_manual(ver)}")
        did_pin = True
        ver2, node2, pnpm2 = _dsh_runtime()
        if ver2 and ver2 != ver:
            ver, node, pnpm = ver2, node2, pnpm2
            continue
        break
    # import-проверка — арбитр полноты замыкания: недостающее добиваем циклом
    entry = _dsh_bridge_entry(profile_dir)
    for _ in range(_DSH_REPAIR_LIMIT):
        ok, missing, detail = _dsh_import_check(node, entry)
        if ok:
            if not did_pin and not did_repair:
                return "hooks plugin present"
            # LOW-2а: работающий DSH держит плагин отключённым до перезапуска
            if did_pin:
                msg = ("hooks plugin re-pinned" if was_present
                       else "hooks plugin installed")
            else:
                msg = "hooks plugin repaired"
            return msg + " (подхватится после полного перезапуска DSH)"
        if not missing:
            return (f"hooks plugin import check failed ({detail}) — "
                    f"проверьте вручную: {_manual(ver)}")
        spec = _dsh_peer_spec(profile_dir, missing, detail, ver)
        if not spec:
            return (f"hooks plugin peer {missing!r} не доустановлен (небезопасное "
                    f"имя или нет range у импортёра) — установите вручную: "
                    f"{_manual(ver)}")
        ok, err = _dsh_pnpm_add(node, pnpm, profile_dir, [spec])
        if not ok:
            return (f"hooks plugin peer install failed (pnpm{err}) — "
                    f"установите вручную: {_manual(ver)}")
        if not _dsh_pkg_version(profile_dir, missing):
            # LOW-2 (security-ревизия): та же пост-сверка, что у основного
            # замыкания (M3) — returncode pnpm не доказательство установки
            return (f"hooks plugin peer install failed (установленный {missing} "
                    f"не читается) — установите вручную: {_manual(ver)}")
        did_repair = True
    return (f"hooks plugin import still failing после {_DSH_REPAIR_LIMIT} "
            f"попыток — установите вручную: {_manual(ver)}")


def dsh_ensure_all_profiles(dry: bool = False) -> list:
    """Сторож hooks-плагина во всех DSH-профилях: для каждого профиля с нашим
    hooks-рядом — _dsh_ensure_hook_plugin (re-pin пакета к фактической
    desktopVersion + import-замыкание). Вызывается треем при старте и раз в
    сутки (native.start_dsh_hook_watchdog): автообновление DSH (rc.X → rc.Y)
    молча отключает плагин rc.X по peer-пинам — захват памяти умирал без
    единой ошибки (инцидент 2026-10-24). При совпадении версий pnpm не
    вызывается, но import-проверка (node -e import) гоняется на каждый
    профиль за прогон. Профилей нет → []; сбой одного профиля не роняет
    остальные. LOW-5 (2026-10-24): «наш ряд» — структурно (_dsh_row_spans +
    _is_our_hooks_row), не подстрока: упоминание hooks-maxgraph в комментарии
    или строковом значении не считается регистрацией. LOW-2: свежий
    runtime.json (<120с) = автообновление DSH в полёте (node_modules может
    быть в середине подмены) — весь прогон откладываем; ручной контур
    (apply_host) этим не затронут."""
    # LOW-2: апдейт DSH в полёте — тяжёлый путь (pnpm) под живым обновлением
    try:
        rt = _home(_DSH_PRIMARY_RUNTIME) / "runtime.json"
        if time.time() - rt.stat().st_mtime < 120:
            return ["runtime.json свежий (DSH обновляется?) — проверка отложена"]
    except OSError:
        pass
    out = []
    for p in sorted(glob.glob(str(_home("~/.dsh/profiles/*/cordis.patch.yml")))):
        try:
            lines = _read_cfg(Path(p)).splitlines(keepends=True)
            ours = any(_is_our_hooks_row(lines[s:e])
                       for s, e, _ind in _dsh_row_spans(lines))
            if not ours:
                continue   # профиль без нашего ряда — не наш клиент
            out.append(f"{p}: " + _dsh_ensure_hook_plugin(Path(p).parent, dry))
        except Exception as ex:
            out.append(f"{p}: ERROR {ex}")
    return out


def _mcp_token() -> str:
    """Токен для MCP-ряда DSH: env MEMORY_TOKEN, иначе data/local_token.txt
    рядом с корнем репо (установщик зовёт скрипт из корня клона, после подъёма
    сервера). Нормализация одинаковая для обоих источников (F6): весь
    whitespace режем — внутренний перевод строки сломает YAML."""
    tok = os.environ.get("MEMORY_TOKEN") or ""
    if not tok.strip():
        try:
            f = Path(__file__).resolve().parent.parent / "data" / "local_token.txt"
            tok = _read_cfg(f)   # utf-8-sig: BOM тут пережил бы split() (\ufeff — не whitespace)
        except OSError:
            return ""
    return "".join(tok.split())


def _dsh_row_spans(lines: list) -> list:
    """(start, end, indent) каждого YAML list-item ('- ' с любым отступом).
    Ряд тянется до следующего item с тем же или меньшим отступом. Строки внутри
    блочных скаляров (key: | / key: >) — не структура (F2)."""
    items = []
    scalar_indent = None
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        ind = len(ln) - len(ln.lstrip(" "))
        if scalar_indent is not None:
            if not stripped or ind > scalar_indent:
                continue            # содержимое блочного скаляра
            scalar_indent = None    # отступ вернулся — скаляр кончился
        if _BLOCK_SCALAR_RE.search(ln):
            scalar_indent = ind
        if stripped.startswith("- ") or stripped == "-":
            items.append((i, ind))
    spans = []
    for k, (i, ind) in enumerate(items):
        end = len(lines)
        for j, ind2 in items[k + 1:]:
            if ind2 <= ind:
                end = j
                break
        spans.append((i, end, ind))
    return spans


def _innermost(hits: list) -> list:
    """Из спанов оставляем самые внутренние (insert-блок, содержащий наш ряд,
    не должен засчитываться как ряд)."""
    return [h for h in hits
            if not any(o is not h and o[0] >= h[0] and o[1] <= h[1] for o in hits)]


def _dsh_mcp_rows(lines: list) -> list:
    """Наши MCP-ряды: [(start, end, mounted)]. mounted = ряд смонтирован через
    insert: (предок с меньшим отступом); голый top-level `- id:` — битая форма
    старой доки, DSH такой ряд молча пропускает (applyEntryPatches: id без
    insert = оверрайд несуществующего ряда)."""
    hits = []
    for s, e, ind in _dsh_row_spans(lines):
        if _is_our_mcp_row(lines[s:e]):
            hits.append((s, e, ind))
    out = []
    for s, e, ind in _innermost(hits):
        mounted = False
        cur = ind
        for k in range(s - 1, -1, -1):   # идём вверх по предкам (отступ < текущего)
            ln = lines[k]
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            li = len(ln) - len(ln.lstrip(" "))
            if li < cur:
                head = ln.split("#", 1)[0].strip()   # L-c: терпим хвостовой комментарий
                if head in ("- insert:", "insert:"):
                    mounted = True
                    break
                cur = li
        out.append((s, e, mounted))
    return out


def _dsh_drop_spans(lines: list, spans: list) -> list:
    """Удалить спаны рядов + опустевшие после этого TOP-LEVEL '- insert:' строки
    (F5: чужой mapping-ключ insert: или вложенный - insert: не трогаем)."""
    for s, e in sorted(spans, reverse=True):
        del lines[s:e]
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip() == "- insert:" and not ln.startswith(" "):
            j = i + 1
            has_child = False
            while j < len(lines):
                nl = lines[j]
                if nl.strip() and not nl.lstrip().startswith("#"):
                    has_child = nl.startswith(" ")
                    break
                j += 1
            if not has_child:
                i += 1
                continue   # пустой insert — мусор
        out.append(ln)
        i += 1
    return out


def _dsh_top_level_ok(text: str) -> bool:
    """M3: cordis.patch.yml — top-level sequence. Любая непустая строка в
    нулевом отступе обязана быть "- " или комментарием; иначе append вслепую
    испортит файл."""
    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if ln.startswith(" "):
            continue
        if not ln.startswith("- "):
            return False
    return True


def _apply_dsh(spec: dict, remove: bool, dry: bool) -> str:
    profiles = sorted(glob.glob(str(_home("~/.dsh/profiles/*/cordis.patch.yml"))))
    hooks_json = _home("~/.maxgraph/hooks.json")
    if not profiles:
        return "skip (no ~/.dsh/profiles/*/cordis.patch.yml)"
    if not remove:
        # hooks.json с Claude-форматом событий, команда — с --host dsh
        data = _load_json(hooks_json)
        if data is None:
            return "skip (~/.maxgraph/hooks.json is not valid JSON)"
        hooks = _hooks_map(data)
        if hooks is None:
            return "skip (~/.maxgraph/hooks.json 'hooks' is not a map)"
        changed = False
        for ev in spec["events"]:
            lst = hooks.setdefault(ev, [])
            ours = next((x for x in lst
                         if MARK in json.dumps(x, ensure_ascii=False)), None)
            if ours is not None:
                # апгрейд устаревшей записи на месте (аудит 2026-10-20:
                # cmd-форма падала в PowerShell — иначе фикс не доезжал бы
                # до уже зарегистрированных DSH: «already registered»)
                for h in (ours.get("hooks") or []):
                    if isinstance(h, dict) and h.get("type") == "command":
                        want = _command("dsh")
                        if h.get("command") != want:
                            h["command"] = want
                            changed = True
                continue
            lst.append({"matcher": ".*", "hooks": [
                {"type": "command", "command": _command("dsh"), "timeout": TIMEOUT}]})
            changed = True
        if changed:
            _write_json(hooks_json, data, dry)
    token = _mcp_token()
    token_ok = bool(token) and bool(_TOKEN_RE.fullmatch(token))   # F1: чужой алфавит не пишем
    results = []
    for prof in profiles:
        try:
            results.append(_dsh_apply_profile(Path(prof), spec, remove, dry,
                                              token, token_ok, hooks_json))
        except Exception as ex:
            # один битый/недекодируемый профиль (напр. UTF-16 от WinPS 5.1) не
            # должен ронять остальные (R2-Low)
            results.append(f"{prof}: ERROR {ex}")
    return "; ".join(results)


def _dsh_apply_profile(p: Path, spec: dict, remove: bool, dry: bool,
                       token: str, token_ok: bool, hooks_json: Path) -> str:
    text = _read_cfg(p) if p.exists() else ""
    lines = text.splitlines(keepends=True)
    if remove:
        # удаляем ТОЛЬКО наши ряды (hooks и mcp) — row-level, чтобы чужие
        # ряды в общем insert-блоке не пострадали; маркеры — только в
        # некомментарных строках и точными значениями (F2)
        spans = _dsh_row_spans(lines)
        drop = []
        for s, e, _ind in spans:
            if _is_our_hooks_row(lines[s:e]) or _is_our_mcp_row(lines[s:e]):
                drop.append((s, e))
        drop = _innermost(drop)
        if not drop:
            return f"{p}: not registered"
        if not dry:
            _atomic_write(p, "".join(_dsh_drop_spans(lines, drop)))
            # пост-проверка (F2): наши маркеры ушли, структура жива — иначе откат
            check = _read_cfg(p)
            check_lines = check.splitlines(keepends=True)
            gone = not _is_our_hooks_row(check_lines) and not _dsh_mcp_rows(check_lines)
            if not gone or not _dsh_top_level_ok(check):
                _restore_text(p, text)
                return f"{p}: ERROR post-check failed — reverted"
        return f"{p}: removed"
    status = []
    add_blocks = []
    # --- hooks ---
    if _is_our_hooks_row(lines):
        status.append("hooks already registered")
    else:
        add_blocks.append(_DSH_BLOCK.format(
            config_path=_yaml_str(str(hooks_json).replace("\\", "/")),
            project_dir=_yaml_str(str(_home("~")).replace("\\", "/"))))
        status.append("hooks registered")
    # --- MCP-ряд: absent → вставка; битый (голый - id:) → переписать ---
    mcp_rows = _dsh_mcp_rows(lines)
    broken = [(s, e) for s, e, mounted in mcp_rows if not mounted]
    dropped = False
    if any(mounted for _s, _e, mounted in mcp_rows):
        status.append("mcp already registered")
    elif not token:
        status.append("mcp skipped (no token — data/local_token.txt)")
    elif not token_ok:
        status.append("mcp skipped (bad token charset)")
    else:
        if broken:
            lines = _dsh_drop_spans(lines, _innermost(broken))
            dropped = True
            status.append("mcp repaired (was bare '- id:', DSH skips those)")
        else:
            status.append("mcp registered")
        add_blocks.append(_DSH_MCP_BLOCK.format(
            url=_yaml_str("http://localhost:8787/mcp"),
            auth=_yaml_str("Bearer " + token)))
    # пакет плагина-моста: без него ряд hooks-maxgraph в конфиге мёртв (см. выше)
    status.append(_dsh_ensure_hook_plugin(p.parent, dry))
    if not add_blocks and not dropped:
        return f"{p}: " + ", ".join(status)
    if not _dsh_top_level_ok(text):
        return f"{p}: skip (top level is not a list — left untouched)"
    if not dry:
        new_text = "".join(lines).rstrip("\n") + "\n" + "".join(add_blocks)
        _atomic_write(p, new_text)
        # пост-проверка: наши блоки на месте, структура верхнего уровня жива,
        # отремонтированных рядов в битой форме не осталось; иначе — откат
        # к содержимому ДО нашей записи (M3)
        check = _read_cfg(p)
        want = (["hooks-maxgraph"] if any("hooks registered" in s for s in status) else []) \
            + (["mcp-maxgraph"] if any(s.startswith("mcp registered")
                                       or s.startswith("mcp repaired") for s in status) else [])
        rows_after = _dsh_mcp_rows(check.splitlines(keepends=True))
        # still_broken судим только когда ремонт реально делался (при
        # skipped-broken битый ряд остаётся нетронутым — это не провал)
        attempted_repair = bool(broken) and any(s.startswith("mcp repaired") for s in status)
        still_broken = attempted_repair and any(not m for _s, _e, m in rows_after)
        if any(w not in check for w in want) or not _dsh_top_level_ok(check) or still_broken:
            _restore_text(p, text)
            return f"{p}: ERROR post-check failed — reverted"
        if os.name != "nt" and "Bearer" in new_text:
            try:
                os.chmod(_real(p), 0o600)   # F3: файл с токеном — только owner
            except OSError:
                pass
    return f"{p}: " + ", ".join(status)


def _detected(spec: dict) -> bool:
    return any(_home(d).exists() for d in spec["detect"])


def apply_host(host: str, remove: bool = False, dry: bool = False) -> str:
    spec = {**HOSTS[host], "_host": host}
    if not _detected(spec):
        return f"{host}: not found"
    if spec["kind"] == "dsh":
        res = _apply_dsh(spec, remove, dry)
    else:
        fn = {"settings": _apply_settings, "flat": _apply_flat,
              "named": _apply_named, "toml": _apply_toml}[spec["kind"]]
        res = fn(_home(spec["config"]), spec, remove, dry)
    msg = f"{host}: {res}"
    if spec.get("note") == "codex-trust" and not remove and "registered" in res:
        msg += "  [скажи пользователю: в Codex один раз выполнить /hooks и доверить хуки]"
    return msg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="MaxGraph capture-hook registrar")
    ap.add_argument("--host", choices=sorted(HOSTS), help="один хост")
    ap.add_argument("--auto", action="store_true", help="все найденные хосты")
    ap.add_argument("--remove", action="store_true", help="снять регистрацию")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if not args.auto and not args.host:
        ap.error("нужен --host X или --auto")
    hosts = sorted(HOSTS) if args.auto else [args.host]
    for h in hosts:
        try:
            print(apply_host(h, remove=args.remove, dry=args.dry_run))
        except Exception as ex:   # один хост не должен ронять остальные
            print(f"{h}: ERROR {ex}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
