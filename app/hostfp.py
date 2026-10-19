#!/usr/bin/env python3
"""Композитный host-fingerprint (hostfp v2).

Чистый stdlib — модуль тестируется отдельно от сервера (как licensing.py).

Железо хоста читает ТОЛЬКО установщик (install.sh / install.ps1): он же
нормализует значения и считает хеши. Наружу (в контейнер и дальше на магазин)
уходят только sha256-хеши — сырые серийники/UUID хост не покидают.

Формат hostfp.json (пишет установщик рядом с compose-файлом):
  {"v": 1, "host_fp": "<64 hex>", "components": {"<tag>": "<64 hex>"},
   "os": "windows|macos|linux", "created": "<iso8601>"}

Хеширование (детерминизм обязателен — тот же алгоритм в установщиках):
  line_i        = "<tag>=<normalized>"
  component_hash = sha256(salt + "|" + line_i)
  host_fp        = sha256(salt + "\\n" + "\\n".join(sorted(line_i)))
Невалидные компоненты (пустые/плейсхолдеры) из fp и скоринга исключаются.

Серверный скоринг частичной смены железа:
  smbios_uuid — «хребет»: валиден с обеих сторон и совпал → та же машина;
  разошёлся → другая машина, скоринг не нужен. Иначе — взвешенный скоринг
  по компонентам, присутствующим с ОБЕИХ сторон: веса board_serial 15,
  disk_serial 15, mac 5 (platform_serial — macOS-аналог board_serial),
  процент = совпавший вес / доступный вес. ≥83% и ≥2 совпавших компонента
  → та же машина; 40–82% (или ≥83% по одному компоненту) → та же машина +
  флаг review; <40% → новая машина.
  os_install_id — идентификатор установки ОС, а не железа: в скоринг не входит.
"""

import hashlib
import hmac
import json
import os
import re
import sys
from pathlib import Path

SALT = "maxgraph-hostfp/v1"

KNOWN_TAGS = frozenset({
    "os_install_id",    # MachineGuid / IOPlatformUUID / machine-id (не скорится)
    "smbios_uuid",      # «хребет» идентичности (переживает переустановку ОС)
    "board_serial",
    "disk_serial",
    "chassis_serial",   # собирается, в скоринге не участвует
    "platform_serial",  # macOS: серийник устройства — аналог board_serial
    "mac",
})

SPINE_TAG = "smbios_uuid"
SCORE_WEIGHTS = {"board_serial": 15, "disk_serial": 15, "mac": 5,
                 "platform_serial": 15}
THRESH_SAME = 83     # ≥ — та же машина
THRESH_REVIEW = 40   # 40–82 — та же машина, но флаг review для админки

FP64_RE = re.compile(r"^[0-9a-f]{64}$")
_OS_SET = frozenset({"windows", "macos", "linux"})
_MAX_COMPONENTS = 16

# Плейсхолдеры прошивок/OEM (после нормализации). all-zero/all-f и
# «только нули и пробелы» ловятся отдельно в is_placeholder.
_PLACEHOLDERS = frozenset({
    "03000200040005000006000700080009",  # дефолтный UUID AMI
    "to be filled by o.e.m.",
    "default string",
    "none",
    "not applicable",
    "n/a",
    "unknown",
    "system serial number",
    "serialnumber",
    "oem",
})

_SEPARATORS = str.maketrans("", "", "-:_")
_WS_RE = re.compile(r"\s+")


def normalize(value) -> str:
    """lowercase, trim, схлопнуть пробелы, срезать разделители - : _ — не-str → ""."""
    if not isinstance(value, str):
        return ""
    v = _WS_RE.sub(" ", value.strip().lower()).translate(_SEPARATORS)
    # translate мог склеить куски без пробелов — это и нужно (uuid/mac/серийники)
    return v.strip()


def is_placeholder(norm: str) -> bool:
    """norm (уже normalize()) — плейсхолдер? Такой компонент из fp исключается."""
    if not norm:
        return True
    if norm in _PLACEHOLDERS:
        return True
    if set(norm) <= set("0"):      # все-нули (uuid/серийники)
        return True
    if set(norm) <= set("f"):      # все-f (незаписанный uuid)
        return True
    if set(norm) <= set("0 "):     # нули и пробелы
        return True
    return False


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def component_hash(tag: str, raw_value) -> str | None:
    """sha256(salt|tag=norm) для одного компонента; невалидное значение → None."""
    norm = normalize(raw_value)
    if not tag or is_placeholder(norm):
        return None
    return _sha256(f"{SALT}|{tag}={norm}")


def compute(components: dict) -> dict:
    """Из сырых {tag: value} → {"host_fp", "components": {tag: hash}}.

    host_fp=None, если валидных компонентов нет совсем (константный fp от пустого
    набора слил бы разные машины в одну).
    """
    hashes, lines = {}, []
    for tag in sorted(components):
        if tag not in KNOWN_TAGS:
            continue
        norm = normalize(components[tag])
        if is_placeholder(norm):
            continue
        line = f"{tag}={norm}"
        hashes[tag] = _sha256(f"{SALT}|{line}")
        lines.append(line)
    host_fp = _sha256(SALT + "\n" + "\n".join(sorted(lines))) if lines else None
    return {"host_fp": host_fp, "components": hashes}


def sanitize_components(obj) -> dict:
    """Клиентские components с границы доверия: только известные теги и 64-hex,
    с потолком. Не-dict → {} (отсутствие компонентов — не ошибка)."""
    if not isinstance(obj, dict):
        return {}
    out = {}
    for tag, h in obj.items():
        if len(out) >= _MAX_COMPONENTS:
            break
        if not isinstance(tag, str) or tag not in KNOWN_TAGS:
            continue
        if not isinstance(h, str) or not FP64_RE.fullmatch(h):
            continue
        out[tag] = h
    return out


def score(old: dict, new: dict) -> tuple:
    """Совпадение двух наборов похомпонентных хешей → (verdict, pct).

    verdict: "same" | "review" | "new". Сравнение только по компонентам,
    присутствующим с обеих сторон; отсутствующие — не совпадение и не
    расхождение. Хребет решает мгновенно; при пустом доступном весе доказательств
    тождества нет → "new" (fail-safe).
    """
    old, new = old or {}, new or {}
    a, b = old.get(SPINE_TAG), new.get(SPINE_TAG)
    if a and b:
        if hmac.compare_digest(a, b):
            return ("same", 100)
        return ("new", 0)
    avail = matched = matched_n = 0
    for tag, w in SCORE_WEIGHTS.items():
        a, b = old.get(tag), new.get(tag)
        if not a or not b:
            continue
        avail += w
        if hmac.compare_digest(a, b):
            matched += w
            matched_n += 1
    if not avail:
        return ("new", 0)
    pct = round(matched * 100 / avail)
    # «same» требует ≥2 независимых совпадений: единственный общий компонент
    # (100% от веса одного mac) — максимум review.
    if pct >= THRESH_SAME and matched_n >= 2:
        return ("same", pct)
    if pct >= THRESH_REVIEW:
        return ("review", pct)
    return ("new", pct)


# ------------------------------------------------------- клиент: hostfp.json ---

def read_hostfp_file(path) -> dict | None:
    """Прочитать и проверить hostfp.json → {"host_fp", "components", "os"?} | None.

    Формат валидируем жёстко, но битый/отсутствующий файл — не ошибка, а None
    (установка работает без привязки). Файл — доверенный локальный (его пишет
    установщик), поэтому перепроверять хеши тут нечем: host_fp из компонентных
    хешей обратно не выводится.
    """
    try:
        p = Path(path)
        if not p.is_file():  # каталог-заглушка от auto-created bind mount — тоже None
            return None
        raw = p.read_text(encoding="utf-8")
        if len(raw) > 8192:
            return None
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != 1:
        return None
    host_fp = data.get("host_fp")
    if not isinstance(host_fp, str) or not FP64_RE.fullmatch(host_fp):
        return None
    components = sanitize_components(data.get("components"))
    out = {"host_fp": host_fp, "components": components}
    osname = data.get("os")
    if osname in _OS_SET:
        out["os"] = osname
    return out


def candidate_paths(env=None) -> list:
    """Где искать hostfp.json: $HOSTFP_PATH → монтирование compose → cwd (python-
    режим из корня репо) → $MEMORY_DIR → data рядом с exe (native) → /data
    (дефолт DATA_DIR python-запуска без MEMORY_DIR). Первый валидный побеждает."""
    env = os.environ if env is None else env
    out = []
    p = (env.get("HOSTFP_PATH") or "").strip()
    if p:
        out.append(p)
    out.append("/app/hostfp.json")  # WORKDIR контейнера; на голом python просто нет файла
    out.append("hostfp.json")
    md = (env.get("MEMORY_DIR") or "").strip()
    if md:
        out.append(str(Path(md) / "hostfp.json"))
    if getattr(sys, "frozen", False):  # native exe: данные рядом с бинарником (как app._default_data_dir)
        out.append(str(Path(sys.executable).resolve().parent / "data" / "hostfp.json"))
    else:
        # python-фолбэк без MEMORY_DIR: ensure_hostfp пишет в DATA_DIR=/data
        # (дефолт app._default_data_dir) — без этого кандидата установка оставалась unanchored
        out.append("/data/hostfp.json")
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def load_hostfp(env=None) -> dict | None:
    """Первый валидный hostfp.json из candidate_paths (или None). Без кеша:
    файл, появившийся после лечения установки, подхватывается следующим вызовом."""
    env = os.environ if env is None else env
    for p in candidate_paths(env):
        data = read_hostfp_file(p)
        if data is not None:
            return data
    return None


def fp_class(env=None) -> str:
    """Класс привязки этой установки: composite (hostfp.json) | legacy (валидный
    env HOST_FP) | unanchored (ни того ни другого — контейнерный fingerprint)."""
    env = os.environ if env is None else env
    if load_hostfp(env) is not None:
        return "composite"
    host_fp = (env.get("HOST_FP") or "").strip().lower()
    if host_fp and FP64_RE.fullmatch(host_fp):
        return "legacy"
    return "unanchored"
