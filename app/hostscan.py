#!/usr/bin/env python3
"""hostscan.py — дообнаружение ИИ-хостов ПОСЛЕ установки MaxGraph.

Установщик (install.sh/install.ps1) подключает хосты один раз — на момент
установки. Хост, появившийся на машине позже (пользователь скачал Claude Code
через месяц), остаётся неподключённым. Этот модуль при каждом запуске программы
ищет такие хосты: конфиг-папка хоста есть, а записи о maxgraph в конфиге нет.

Детекция НЕ дублируется: вся таблица хостов и проверки «уже подключён» живут в
hooks/register_hook.py — мы прогоняем его apply_host(host, dry=True) (dry-run
ничего не пишет) и читаем вердикт из строки-результата. Само подключение — тоже
его код (native.connect_hosts), здесь только обнаружение.

Дёшево: ~15 stat() по фиксированным путям (~/.claude и т.п.) + чтение маленьких
конфигов найденных хостов. Сканирования диска нет.

Идемпотентность уведомления: data/host-scan.json {"notified": {host: iso}} —
балун о хосте показывается один раз; «незаконнекченность» сама по себе стейт не
трогает (пункт меню трея показывается, пока хост реально не подключён).

Чистый stdlib — модуль тестируется отдельно от сервера (как hostfp.py).
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_NAME = "host-scan.json"


# ------------------------------------------------------- регистратор (reuse)

def _registrar_candidates() -> list:
    """Где искать hooks/register_hook.py: бандл exe (_MEIPASS/hooks) → исходники
    (<repo>/hooks). Первый существующий побеждает. В frozen exe repo-фолбэк
    ЗАПРЕЩЁН (security MED-1): __file__ PYZ-модуля в onefile указывает в %TEMP%,
    предсказуемый путь %TEMP%/hooks/register_hook.py — точка подмены кода,
    исполняемого при каждом старте."""
    out = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(Path(meipass) / "hooks" / "register_hook.py")
    if not getattr(sys, "frozen", False):
        out.append(Path(__file__).resolve().parent.parent / "hooks" / "register_hook.py")
    return out


def load_registrar(path=None):
    """Загрузить hooks/register_hook.py как модуль (importlib, имя приватное —
    в sys.modules не светится). Файла нет/битый → None: фича молча выключена."""
    paths = [Path(path)] if path else _registrar_candidates()
    for p in paths:
        try:
            if not p.is_file():
                continue
            spec = importlib.util.spec_from_file_location("mg_register_hook", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        except Exception:
            continue
    return None


# ------------------------------------------------------------- обнаружение

# shortcut: вердикт парсится из человекочитаемых строк apply_host — пере-
# именование вердиктов в register_hook.py молча сломает детект; страховка —
# тест test_register_hook_verdict_words_alive (test_hostscan.py). Апгрейд:
# структурный результат из apply_host.
def _would_connect(res: str) -> bool:
    """Вердикт из строки apply_host(dry=True): хост найден и регистрация ЧТО-ТО
    записала бы. «already registered»/«not registered»/«skip (...)» — не считаем."""
    t = res.replace("already registered", "").replace("not registered", "")
    return ("registered" in t) or ("repaired" in t)


def find_unconnected(rh, apply_host=None) -> list:
    """Хосты: конфиг-папка есть, записи о maxgraph нет. rh — модуль register_hook
    (load_registrar); apply_host — точка инъекции для тестов. Сбой одного хоста
    не роняет остальные (как main() самого регистратора)."""
    apply_host = apply_host or rh.apply_host
    out = []
    for host in sorted(rh.HOSTS):
        try:
            res = apply_host(host, dry=True)
        except Exception:
            continue
        if res.endswith(": not found"):
            continue
        if _would_connect(res):
            out.append(host)
    return out


# ------------------------------------------------------------------ стейт

def load_state(data_dir) -> dict:
    """{"notified": {host: iso}}; файла нет/битый/чужая форма → пустой стейт."""
    try:
        data = json.loads((Path(data_dir) / STATE_NAME).read_text(encoding="utf-8"))
        notified = data.get("notified")
        if isinstance(notified, dict):
            return {"notified": {str(k): str(v) for k, v in notified.items()}}
    except Exception:
        pass
    return {"notified": {}}


def save_state(data_dir, state: dict) -> None:
    """Атомарно (tmp по pid + fsync + os.replace) — тот же приём, что у
    регистратора (F4)."""
    f = Path(data_dir) / STATE_NAME
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(state, indent=1))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, f)


def scan(data_dir, rh=None, apply_host=None) -> dict:
    """Один проход при старте → {"unconnected": [...], "notify": [...]}.

    unconnected — все найденные неподключённые хосты (для пункта меню трея).
    notify — подмножество, о котором ЕЩЁ не сообщали (для балуна); помечается
    в стейте сразу — повторный запуск балун не дублирует. Регистратора нет
    (серверная установка без hooks/) → обе пустые.
    """
    rh = rh if rh is not None else load_registrar()
    if rh is None:
        return {"unconnected": [], "notify": []}
    unconnected = find_unconnected(rh, apply_host=apply_host)
    state = load_state(data_dir)
    notify = [h for h in unconnected if h not in state["notified"]]
    if notify:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for h in notify:
            state["notified"][h] = now
        try:
            save_state(data_dir, state)
        except OSError:
            pass   # стейт не записался — балун повторится при след. старте, не страшно
    return {"unconnected": unconnected, "notify": notify}
