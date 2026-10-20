#!/usr/bin/env python3

















































import json
import logging
import math
import os
import threading
import time

log = logging.getLogger("maxgraph.actr")

DEFAULT_FLUSH_SEC = 300
DEFAULT_MAX_BOOST = 0.25
DEFAULT_HALF_LIFE_DAYS = 14.0
TOUCH_TOP = 20




def _int_env(value, default: int) -> int:


    try:
        v = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _unit_float_env(value, default: float) -> float:


    try:
        v = float(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    if v != v:
        return default
    return min(1.0, max(0.0, v))


def _pos_float_env(value, default: float) -> float:


    try:
        v = float(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    if v != v or v <= 0.0 or not math.isfinite(v):
        return default
    return v


def config_from_env(env=None) -> dict:

    env = os.environ if env is None else env
    return {
        "flush_sec": _int_env(env.get("ACTR_FLUSH_SEC"), DEFAULT_FLUSH_SEC),
        "max_boost": _unit_float_env(env.get("ACTR_MAX_BOOST"), DEFAULT_MAX_BOOST),
        "half_life_days": _pos_float_env(env.get("ACTR_HALF_LIFE_DAYS"),
                                         DEFAULT_HALF_LIFE_DAYS),
    }





_warned = set()
_warn_lock = threading.Lock()


def _warn_once(key: str, msg: str, *args) -> None:
    with _warn_lock:
        if key in _warned:
            return
        _warned.add(key)
    log.warning(msg, *args)




def boost(n, last_ts, now=None, *, half_life_days=DEFAULT_HALF_LIFE_DAYS) -> float:


    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0.0
    hl = _pos_float_env(half_life_days, DEFAULT_HALF_LIFE_DAYS)
    try:
        ts = float(last_ts)
    except (TypeError, ValueError):
        ts = 0.0
    if not math.isfinite(ts) or ts <= 0.0:
        ts = 0.0
    cur = time.time() if now is None else float(now)
    age_days = max(0.0, (cur - ts) / 86400.0) if ts else 0.0
    return math.log1p(n) * (0.5 ** (age_days / hl))


def multiplier(n, last_ts, now=None, *, max_boost=DEFAULT_MAX_BOOST,
               half_life_days=DEFAULT_HALF_LIFE_DAYS) -> float:


    b = min(boost(n, last_ts, now=now, half_life_days=half_life_days), 1.0)
    try:
        mb = float(max_boost)
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_BOOST
    if not math.isfinite(mb) or mb <= 0.0:
        return 1.0 + b * 0.0
    return 1.0 + b * min(1.0, mb)


def boost_fn(store, cfg=None):


    cfg = config_from_env() if cfg is None else cfg
    mb, hl = cfg["max_boost"], cfg["half_life_days"]

    def mult(name):
        n, t = store.get(name)
        return multiplier(n, t, max_boost=mb, half_life_days=hl)

    return mult







def _load_file(path) -> dict:


    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        _warn_once("load", "actr: %s не читается (%s) — счётчики с нуля "
                   "(fail-open)", path, e)
        return {}
    out = {}
    if isinstance(raw, dict):
        for name, rec in raw.items():
            try:
                if not isinstance(rec, dict):
                    continue
                n, t = rec.get("n"), rec.get("t")
                if (isinstance(n, bool) or not isinstance(n, int) or n < 0
                        or isinstance(t, bool)
                        or not isinstance(t, (int, float))
                        or not math.isfinite(float(t)) or t < 0):
                    continue
                out[str(name)] = {"n": n, "t": float(t)}
            except (TypeError, ValueError):
                continue
    return out


def _save_file(path, data) -> bool:


    p = str(path)
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)
        return True
    except Exception as e:
        _warn_once("save", "actr: не удалось записать %s: %s", p, e)
        return False


class Store:






    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def touch(self, names, now=None) -> int:



        ts = time.time() if now is None else float(now)
        data = self._data
        n_ok = 0
        with self._lock:
            for name in names or ():
                name = str(name or "").strip()
                if not name:
                    continue
                rec = data.get(name)
                if rec is None:
                    data[name] = {"n": 1, "t": ts}
                else:
                    rec["n"] += 1
                    rec["t"] = ts
                n_ok += 1
        return n_ok

    def get(self, name):

        with self._lock:
            rec = self._data.get(str(name or ""))
            return (0, 0.0) if rec is None else (rec["n"], rec["t"])

    def clear(self) -> None:

        with self._lock:
            self._data.clear()

    def load(self, path) -> int:

        data = _load_file(path)
        with self._lock:
            self._data = data
        return len(data)

    def flush(self, path) -> bool:

        with self._lock:
            snap = {k: dict(v) for k, v in self._data.items()}
        return _save_file(path, snap)
