#!/usr/bin/env python3





























import hashlib
import hmac
import json
import os
import re
import sys
from pathlib import Path

SALT = "maxgraph-hostfp/v1"

KNOWN_TAGS = frozenset({
    "os_install_id",
    "smbios_uuid",
    "board_serial",
    "disk_serial",
    "chassis_serial",
    "platform_serial",
    "mac",
})

SPINE_TAG = "smbios_uuid"
SCORE_WEIGHTS = {"board_serial": 15, "disk_serial": 15, "mac": 5,
                 "platform_serial": 15}
THRESH_SAME = 83
THRESH_REVIEW = 40

FP64_RE = re.compile(r"^[0-9a-f]{64}$")
_OS_SET = frozenset({"windows", "macos", "linux"})
_MAX_COMPONENTS = 16



_PLACEHOLDERS = frozenset({
    "03000200040005000006000700080009",
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

    if not isinstance(value, str):
        return ""
    v = _WS_RE.sub(" ", value.strip().lower()).translate(_SEPARATORS)

    return v.strip()


def is_placeholder(norm: str) -> bool:

    if not norm:
        return True
    if norm in _PLACEHOLDERS:
        return True
    if set(norm) <= set("0"):
        return True
    if set(norm) <= set("f"):
        return True
    if set(norm) <= set("0 "):
        return True
    return False


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def component_hash(tag: str, raw_value) -> str | None:

    norm = normalize(raw_value)
    if not tag or is_placeholder(norm):
        return None
    return _sha256(f"{SALT}|{tag}={norm}")


def compute(components: dict) -> dict:





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


    if pct >= THRESH_SAME and matched_n >= 2:
        return ("same", pct)
    if pct >= THRESH_REVIEW:
        return ("review", pct)
    return ("new", pct)




def read_hostfp_file(path) -> dict | None:







    try:
        p = Path(path)
        if not p.is_file():
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



    env = os.environ if env is None else env
    out = []
    p = (env.get("HOSTFP_PATH") or "").strip()
    if p:
        out.append(p)
    out.append("/app/hostfp.json")
    out.append("hostfp.json")
    md = (env.get("MEMORY_DIR") or "").strip()
    if md:
        out.append(str(Path(md) / "hostfp.json"))
    if getattr(sys, "frozen", False):
        out.append(str(Path(sys.executable).resolve().parent / "data" / "hostfp.json"))
    else:


        out.append("/data/hostfp.json")
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def load_hostfp(env=None) -> dict | None:


    env = os.environ if env is None else env
    for p in candidate_paths(env):
        data = read_hostfp_file(p)
        if data is not None:
            return data
    return None


def fp_class(env=None) -> str:


    env = os.environ if env is None else env
    if load_hostfp(env) is not None:
        return "composite"
    host_fp = (env.get("HOST_FP") or "").strip().lower()
    if host_fp and FP64_RE.fullmatch(host_fp):
        return "legacy"
    return "unanchored"
