#!/usr/bin/env python3
"""
maxgraph_hook.py — универсальный захват-хук Maxgraph (Ф5, MEMORY-SYSTEM-v2).
Чистый stdlib, Python 3.10+. Установка: hooks/README.md.

stdin: JSON события хука (формат Claude Code / DSH dsh-hooks-claude-code):
  {"hook_event_name": "PostToolUse", "session_id": …, "cwd": …, "tool_name": …,
   "tool_input": {…}, "tool_response": …}  |  {"hook_event_name": "UserPromptSubmit",
   "prompt": …}  |  SessionStart {"source": …}  |  SessionEnd {"reason": …,
   "open_tasks": ["…"]? (В2.1, опционально)}  |  Stop
→ нормализация {"hook","project","ts","data"} → append в локальный spool
(~/.maxgraph/spool.jsonl) → попытка POST всего накопленного батчами по 100 на
$MG_SERVER/api/ingest (Bearer $MG_TOKEN, таймаут 3с).

Fail-open (референс: claude-mem — transport errors → exit 0, хук никогда не
блокирует хоста): любая ошибка сети/HTTP≠200 → события молча остаются в spool и
доставятся при следующем запуске; скрипт ВСЕГДА завершается кодом 0.

Идемпотентность: content-hash (SHA256 канонического {hook,project,ts,data} —
та же формула, что app.py::_ingest_hash): дедуп внутри spool при flush +
последние 1000 хэшей на сервере (data/ingest-hashes.json) → повторная доставка
не дублирует. ts генерируется один раз при нормализации → редоставка имеет тот
же хэш.

Env:
  MG_SERVER  — базовый URL сервера (https://memory.example.com); пусто → только spool
  MG_TOKEN   — bearer-токен (= MEMORY_TOKEN сервера)
  MG_SPOOL   — путь spool-файла (default ~/.maxgraph/spool.jsonl)

Ротация spool: при превышении 10MB остаётся хвост ~5MB (целые строки).
Проект — каскад Ф1: git remote → git root → имя cwd (kebab-slug), сервер
принимает готовый slug.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOOKS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd")
TIMEOUT = 3                     # сек на POST — хук не блокирует хоста
GIT_TIMEOUT = 2                 # сек на git-вызов детекта проекта
BATCH = 100                     # лимит батча /api/ingest
SPOOL_MAX = 10 * 1024 * 1024    # ротация spool: 10MB
SPOOL_KEEP = SPOOL_MAX // 2     # …после ротации остаётся хвост 5MB
FIELD_MAX = 2000                # потолок символов на текстовое поле data


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def spool_path() -> Path:
    return Path(_env("MG_SPOOL") or "~/.maxgraph/spool.jsonl").expanduser()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- project ----

_SLUG_BAD = re.compile(r"[^a-z0-9]+")


def slugify(text) -> str:
    """Любая строка → kebab-slug (≤64), совместимый с серверным _check_project."""
    return _SLUG_BAD.sub("-", str(text or "").strip().lower()).strip("-")[:64].strip("-")


def _git_repo_name(url: str) -> str:
    # git@github.com:RuMemory/maxgraph.git | https://github.com/RuMemory/maxgraph.git
    tail = re.split(r"[/:]", str(url).strip().rstrip("/"))[-1]
    return slugify(re.sub(r"\.git$", "", tail))


def _run_git(cmd: list) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"git rc={r.returncode}")
    return (r.stdout or "").strip()


def detect_project(cwd: str = "", runner=None) -> str:
    """Каскад Ф1: git remote → git root → имя cwd. Сбой любого шага → следующий;
    пустой cwd → "" (плоский граф). runner инжектится в тестах."""
    if not cwd:
        return ""
    run = runner or _run_git
    try:
        name = _git_repo_name(run(["git", "-C", cwd, "remote", "get-url", "origin"]))
        if name:
            return name
    except Exception:
        pass
    try:
        root = run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
        name = slugify(Path(root).name)
        if name:
            return name
    except Exception:
        pass
    return slugify(Path(cwd).name)


# ------------------------------------------------------------- normalize -----

def _cut(v, n: int):
    """Поле data: dict/list → компактный JSON, прочее → str; потолок n символов."""
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    else:
        s = str(v)
    return s[:n]


def normalize(payload: dict) -> dict:
    """Событие хука Claude Code/DSH → {"hook","project","ts","data"}.
    Неизвестный/битый хук → None (не спулим мусор)."""
    if not isinstance(payload, dict):
        return None
    hook = payload.get("hook_event_name") or payload.get("hook") or ""
    if hook not in HOOKS:
        return None
    data = {}
    if hook == "PostToolUse":
        data = {"tool": _cut(payload.get("tool_name"), 100),
                "input": _cut(payload.get("tool_input"), FIELD_MAX),
                "response": _cut(payload.get("tool_response"), FIELD_MAX)}
    elif hook == "UserPromptSubmit":
        data = {"prompt": _cut(payload.get("prompt"), FIELD_MAX)}
    elif hook == "SessionStart":
        data = {"source": _cut(payload.get("source"), 100)}
    elif hook == "SessionEnd":
        data = {"reason": _cut(payload.get("reason"), 100)}
        # В2.1 (work_state): хост может передать открытые дела сессии — сервер
        # создаст task-сущности. Только список строк, ≤20 шт, ≤200 символов.
        ot = payload.get("open_tasks")
        if isinstance(ot, list):
            data["open_tasks"] = [_cut(t, 200) for t in ot[:20] if isinstance(t, str)]
    # Stop — без data (stop_hook_active неинформативен для памяти)
    return {"hook": hook, "project": detect_project(payload.get("cwd") or ""),
            "ts": _now(), "data": data}


def content_hash(event: dict) -> str:
    """SHA256 канонического {hook,project,ts,data} — формула 1-в-1 как
    app.py::_ingest_hash (sort_keys, ensure_ascii=False): дедуп spool и сервера
    согласованы."""
    canon = json.dumps({"hook": event.get("hook", ""), "project": event.get("project", ""),
                        "ts": event.get("ts", ""), "data": event.get("data", {})},
                       sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- spool -----

def _read_lines(p: Path) -> list:
    """Непустые строки spool'а (сырые); отсутствующий/нечитабельный файл → []."""
    try:
        return [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []


def _parse_events(lines: list) -> list:
    """Сырые строки → валидные dict-события (битые строки пропускаем)."""
    out = []
    for line in lines:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def append_spool(event: dict, path=None) -> None:
    """Append одной JSONL-строки + мягкая ротация: >10MB → хвост ~5MB целых строк."""
    p = path or spool_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    try:
        if p.stat().st_size > SPOOL_MAX:
            data = p.read_bytes()[-SPOOL_KEEP:]
            nl = data.find(b"\n")
            p.write_bytes(data[nl + 1:] if 0 <= nl < len(data) - 1 else data)
    except OSError:
        pass  # ротация не должна ронять хук


def load_spool(path=None) -> list:
    """Все валидные события spool'а (битые строки пропускаем)."""
    return _parse_events(_read_lines(path or spool_path()))


def _drop_lines(p: Path, n: int) -> None:
    """После успешной доставки убрать первые n строк, СОХРАНИВ хвост: другой
    hook-процесс мог дописать события, пока мы слали (unlink потерял бы их)."""
    try:
        lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        rest = lines[n:]
        if rest:
            p.write_text("\n".join(rest) + "\n", encoding="utf-8")
        else:
            p.unlink(missing_ok=True)
    except OSError:
        pass


# ----------------------------------------------------------------- flush -----

def _http_post(url: str, token: str, payload: dict, timeout: int) -> int:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def flush(server: str = "", token: str = "", path=None, http_post=None):
    """Доставка spool'а батчами по 100 на {server}/api/ingest.

    → {"sent","dedup","batches"} при полном успехе (отправленные строки сняты со
    spool'а); None — без конфига (нет MG_SERVER/MG_TOKEN), сетевой/HTTP-ошибке
    (fail-open: spool НЕ трогаем, доставим при следующем запуске; повторная
    доставка безопасна — сервер дедупит по content-hash). Частичный успех
    (батч 1 ок, батч 2 упал) тоже → None и spool целиком на месте: серверная
    дедупликация делает редоставку бесплатной.
    """
    server = (server or _env("MG_SERVER")).rstrip("/")
    token = token or _env("MG_TOKEN")
    if not server or not token:
        return None
    p = path or spool_path()
    raw_lines = _read_lines(p)
    if not raw_lines:
        return {"sent": 0, "dedup": 0, "batches": 0}
    events = _parse_events(raw_lines)
    # дедуп внутри spool'а по content-hash (та же формула, что на сервере)
    seen, uniq = set(), []
    for ev in events:
        h = content_hash(ev)
        if h in seen:
            continue
        seen.add(h)
        uniq.append(ev)
    post = http_post or _http_post
    try:
        sent = batches = 0
        for i in range(0, len(uniq), BATCH):
            chunk = uniq[i:i + BATCH]
            if post(f"{server}/api/ingest", token, {"events": chunk}, TIMEOUT) != 200:
                return None
            sent += len(chunk)
            batches += 1
    except Exception:
        return None  # fail-open: сеть/сервер недоступны — spool остаётся
    _drop_lines(p, len(raw_lines))
    return {"sent": sent, "dedup": len(events) - len(uniq), "batches": batches}


# ------------------------------------------------------------------ main -----

def main(stdin=None) -> int:
    """stdin JSON → spool → попытка flush. ВСЕГДА 0: хук не должен ломать хоста
    (claude-mem: transport errors → exit 0). Даже при неизвестном хуке делаем
    flush — drain накопленного spool'а на каждом вызове."""
    try:
        src = stdin if stdin is not None else sys.stdin
        raw = src.read()
        payload = json.loads(raw) if raw.strip() else {}
        ev = normalize(payload)
        if ev is not None:
            append_spool(ev)
        flush()  # читает MG_SERVER/MG_TOKEN; без конфига — молчаливый no-op
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
