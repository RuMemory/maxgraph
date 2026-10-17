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
import shutil
import sys
from pathlib import Path

MARK = "maxgraph_hook.py"   # метка наших записей во всех форматах
TIMEOUT = 10


def _home(p: str) -> Path:
    return Path(p).expanduser()


def _command(host: str) -> str:
    """Команда вызова хука. Windows: pythonw (без окна консоли) + полный путь —
    в JSON-конфигах ~ и %USERPROFILE% не везде разворачиваются."""
    hook = _home("~/.maxgraph/maxgraph_hook.py")
    if os.name == "nt":
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
    """YAML double-quoted скаляр; пути уже с forward slashes."""
    return '"' + s.replace('"', '\\"') + '"'


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


def _load_json(path: Path):
    """dict или None: файла нет → {} (создадим), битый/не-dict → None (не трогаем)."""
    path = _real(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _backup_once(path: Path) -> None:
    path = _real(path)
    bak = path.with_name(path.name + ".bak-maxgraph")
    if path.exists() and not bak.exists():
        try:
            bak.write_bytes(path.read_bytes())
        except OSError:
            pass


def _atomic_write(path: Path, text: str) -> None:
    """tmp + os.replace в цель симлинка; права исходного сохраняем (в конфигах
    бывают API-ключи с 0600 — не размягчаем до umask)."""
    path = _real(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_once(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
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
    .bak-maxgraph — он со времени первой записи мог устареть)."""
    try:
        _real(path).write_text(text, encoding="utf-8")
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
    text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
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

# Наши MCP-ряды: id из актуальной доки и из старой (remotemem-эпохи PAIRING.md)
_MCP_IDS = ("id: mcp-maxgraph", "id: mcp-remotemem")
_MCP_SERVERS = ("serverName: maxgraph", "serverName: remotemem")


def _mcp_token() -> str:
    """Токен для MCP-ряда DSH: env MEMORY_TOKEN, иначе data/local_token.txt
    рядом с корнем репо (установщик зовёт скрипт из корня клона, после подъёма
    сервера). Весь whitespace режем: внутренний перевод строки сломает YAML."""
    tok = (os.environ.get("MEMORY_TOKEN") or "").strip()
    if tok:
        return tok
    try:
        f = Path(__file__).resolve().parent.parent / "data" / "local_token.txt"
        return "".join(f.read_text(encoding="utf-8").split())
    except OSError:
        return ""


def _dsh_row_spans(lines: list) -> list:
    """(start, end, indent) каждого YAML list-item ('- ' с любым отступом).
    Ряд тянется до следующего item с тем же или меньшим отступом."""
    items = [(i, len(ln) - len(ln.lstrip(" ")))
             for i, ln in enumerate(lines)
             if ln.strip().startswith("- ") or ln.strip() == "-"]
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
        text = "".join(lines[s:e])
        if "dsh-mcp-client" not in text:
            continue
        if any(m in text for m in _MCP_SERVERS) or any(m in text for m in _MCP_IDS):
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
                if ln.strip() in ("- insert:", "insert:"):
                    mounted = True
                    break
                cur = li
        out.append((s, e, mounted))
    return out


def _dsh_drop_spans(lines: list, spans: list) -> list:
    """Удалить спаны рядов + опустевшие после этого строки '- insert:'."""
    for s, e in sorted(spans, reverse=True):
        del lines[s:e]
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip() in ("- insert:", "insert:"):
            ind = len(ln) - len(ln.lstrip(" "))
            j = i + 1
            has_child = False
            while j < len(lines):
                nl = lines[j]
                if nl.strip() and not nl.lstrip().startswith("#"):
                    if len(nl) - len(nl.lstrip(" ")) > ind:
                        has_child = True
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
            if any(MARK in json.dumps(x, ensure_ascii=False) for x in lst):
                continue
            lst.append({"matcher": ".*", "hooks": [
                {"type": "command", "command": _command("dsh"), "timeout": TIMEOUT}]})
            changed = True
        if changed:
            _write_json(hooks_json, data, dry)
    token = _mcp_token()
    results = []
    for prof in profiles:
        p = Path(prof)
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        lines = text.splitlines(keepends=True)
        active = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        if remove:
            # удаляем ТОЛЬКО наши ряды (hooks и mcp) — row-level, чтобы чужие
            # ряды в общем insert-блоке не пострадали
            spans = _dsh_row_spans(lines)
            drop = []
            for s, e, _ind in spans:
                block_text = "".join(lines[s:e])
                is_hooks = any("hooks-maxgraph" in l for l in lines[s:e])
                is_mcp = ("dsh-mcp-client" in block_text
                          and any(m in block_text for m in _MCP_SERVERS)) \
                    or any(m in block_text for m in _MCP_IDS)
                if is_hooks or is_mcp:
                    drop.append((s, e))
            drop = _innermost(drop)
            if not drop:
                results.append(f"{p}: not registered")
                continue
            if not dry:
                _atomic_write(p, "".join(_dsh_drop_spans(lines, drop)))
            results.append(f"{p}: removed")
            continue
        status = []
        add_blocks = []
        # --- hooks ---
        if any("hooks-maxgraph" in ln for ln in active):
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
        if not add_blocks and not dropped:
            results.append(f"{p}: " + ", ".join(status))
            continue
        if not _dsh_top_level_ok(text):
            results.append(f"{p}: skip (top level is not a list — left untouched)")
            continue
        if not dry:
            new_text = "".join(lines).rstrip("\n") + "\n" + "".join(add_blocks)
            _atomic_write(p, new_text)
            # пост-проверка: наши блоки на месте, структура верхнего уровня жива,
            # отремонтированных рядов в битой форме не осталось; иначе — откат
            # к содержимому ДО нашей записи (M3)
            check = p.read_text(encoding="utf-8")
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
                results.append(f"{p}: ERROR post-check failed — reverted")
                continue
        results.append(f"{p}: " + ", ".join(status))
    return "; ".join(results)


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
