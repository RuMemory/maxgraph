#!/usr/bin/env python3











































import base64
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import time
import uuid
from pathlib import Path

TRIAL_DAYS_DEFAULT = 7
TIERS = ("pro", "team")
STATE_NAME = "license.json"
KEY_NAME = ".hmac_key"
MEMORY_NAMES = ("memory.json", "memory.ndjson")
_DAY = 86400




def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hmac_hex(secret: str, data: str) -> str:
    return hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()


def _canon(first_seen: float, last_seen: float, license_) -> str:
    return json.dumps({"first_seen": first_seen, "last_seen": last_seen, "license": license_},
                      sort_keys=True, separators=(",", ":"))


def _sign_state(secret: str, st: dict) -> str:
    return _hmac_hex(secret, _canon(st["first_seen"], st["last_seen"], st.get("license")))


def get_secret(data_dir, env=None) -> str:

    env = os.environ if env is None else env
    val = (env.get("LICENSE_HMAC_SECRET") or "").strip()
    if val:
        return val
    key_path = Path(data_dir) / KEY_NAME
    if key_path.exists():
        saved = key_path.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    saved = secrets.token_hex(32)
    key_path.write_text(saved, encoding="utf-8")
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return saved


def trial_days(env=None) -> int:
    env = os.environ if env is None else env
    try:
        n = int(env.get("TRIAL_DAYS", "") or TRIAL_DAYS_DEFAULT)
    except (TypeError, ValueError):
        return TRIAL_DAYS_DEFAULT
    return n if n > 0 else TRIAL_DAYS_DEFAULT


def _memory_nonempty(data_dir) -> bool:
    for name in MEMORY_NAMES:
        p = Path(data_dir) / name
        try:
            if p.exists() and p.stat().st_size > 0 and p.read_text(encoding="utf-8", errors="ignore").strip():
                return True
        except OSError:
            continue
    return False


def _state_path(data_dir) -> Path:
    return Path(data_dir) / STATE_NAME


def _load_state(data_dir) -> dict | None:
    p = _state_path(data_dir)
    if not p.exists():
        return None
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(st, dict) or "first_seen" not in st or "last_seen" not in st:
        return None
    return st


def _save_state(data_dir, st: dict, secret: str):
    st["sig"] = _sign_state(secret, st)
    p = _state_path(data_dir)
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _state_valid(st: dict, secret: str) -> bool:
    sig = st.get("sig") or ""
    expect = _sign_state(secret, st)
    return bool(sig) and hmac.compare_digest(sig, expect)


def _new_state(now: float) -> dict:
    return {"first_seen": now, "last_seen": now, "license": None,
            "latest": None, "latest_checked": None}


def version_newer(latest, current) -> bool:

    def _parse(v):
        try:
            parts = str(v).strip().lstrip("vV").split(".")
            nums = tuple(int(x) for x in parts[:4])
            return nums if nums and all(n >= 0 for n in nums) else None
        except (ValueError, TypeError):
            return None
    a, b = _parse(latest), _parse(current)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


def set_latest(data_dir, secret: str, version: str, now: float | None = None) -> bool:







    now = time.time() if now is None else now
    st = _load_state(data_dir)
    if st is None or not _state_valid(st, secret):
        if _memory_nonempty(data_dir):
            return False
        st = _new_state(now)
    st["latest"] = str(version)[:32]
    st["latest_checked"] = now
    _save_state(data_dir, st, secret)
    return True


def latest_version(data_dir, secret: str) -> str | None:

    st = _load_state(data_dir)
    if st is None or not _state_valid(st, secret):
        return None
    return st.get("latest")




def reissue_key(secret: str, email: str, tier: str, exp: int, now: float | None = None) -> str:





    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}")
    now = time.time() if now is None else now
    payload = {"email": email, "tier": tier, "iat": int(now), "exp": int(exp or 0)}
    blob = _b64e(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"mg_{blob}.{_hmac_hex(secret, blob)}"


def generate_key(secret: str, email: str, tier: str, days: int, now: float | None = None) -> str:

    now = time.time() if now is None else now
    return reissue_key(secret, email, tier, 0 if days <= 0 else int(now + days * _DAY), now=now)


def verify_key(secret: str, key: str, now: float | None = None):

    now = time.time() if now is None else now
    key = (key or "").strip()
    if not key.startswith("mg_") or "." not in key:
        return None, "bad format (ожидается mg_<payload>.<signature>)"
    blob, _, sig = key[3:].rpartition(".")
    try:
        payload = json.loads(_b64d(blob).decode("utf-8"))
    except Exception:
        return None, "bad payload (не base64url-json)"
    if not isinstance(payload, dict):
        return None, "bad payload"
    if not hmac.compare_digest(sig, _hmac_hex(secret, blob)):
        return None, "bad signature"
    if payload.get("tier") not in TIERS:
        return None, f"unknown tier (нужен {'|'.join(TIERS)})"
    exp = payload.get("exp") or 0
    if exp and now > exp:
        return None, "license expired"
    return payload, None













RELAY_TOKEN_TTL = 7 * _DAY


def relay_secret(secret: str, env=None) -> str:



    env = os.environ if env is None else env
    val = (env.get("RELAY_HMAC_SECRET") or "").strip()
    if val:
        return val
    return hmac.new(secret.encode("utf-8"), b"maxgraph-relay-v1", hashlib.sha256).hexdigest()


def relay_token(secret: str, license_key: str, exp: int) -> str:


    payload = {"lk": hashlib.sha256(license_key.encode("utf-8")).hexdigest()[:16],
               "exp": int(exp)}
    p64 = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{p64}.{_hmac_hex(secret, 'relay:' + p64)}"


REVOKE_GRACE_SEC = 72 * 3600




def status(data_dir, secret: str, now: float | None = None) -> dict:






    now = time.time() if now is None else now
    st = _load_state(data_dir)

    if st is None:

        if _memory_nonempty(data_dir):
            return {"state": "expired", "days_left": 0,
                    "reason": "license.json отсутствует/повреждён при непустой памяти (fail-closed)"}
        st = _new_state(now)
        _save_state(data_dir, st, secret)
    elif not _state_valid(st, secret):
        if _memory_nonempty(data_dir):
            return {"state": "expired", "days_left": 0,
                    "reason": "подпись license.json не сошлась при непустой памяти (fail-closed)"}
        st = _new_state(now)
        _save_state(data_dir, st, secret)


    if now < st["last_seen"]:
        out = {"state": "expired", "days_left": 0, "reason": "clock rollback (now < last_seen)"}
        lic = st.get("license")
        if lic:
            out.update({"email": lic.get("email"), "tier": lic.get("tier"), "exp": lic.get("exp")})
        return out

    lic = st.get("license")
    if lic:
        exp = lic.get("exp") or 0
        out = {"email": lic.get("email"), "tier": lic.get("tier"), "exp": exp}
        if lic.get("key"):
            out["key"] = lic["key"]
        revoked_at = lic.get("revoked_at") or 0
        if revoked_at:
            grace_ends = revoked_at + REVOKE_GRACE_SEC
            if now > grace_ends:
                out.update({"state": "expired", "days_left": 0, "revoked": True,
                            "reason": "license revoked (grace 72h ended)"})
                return out
            out["revoked"] = True
            out["grace_ends"] = int(grace_ends)
        if exp and now > exp:
            out.update({"state": "expired", "days_left": 0, "reason": "license expired"})
        else:
            out["state"] = "licensed"
            out["days_left"] = max(0, -(-(exp - now) // _DAY)) if exp else None
    else:
        remaining = st["first_seen"] + trial_days() * _DAY - now
        if remaining > 0:
            out = {"state": "trial", "days_left": max(1, -(-int(remaining) // _DAY))}
        else:
            out = {"state": "expired", "days_left": 0, "reason": "trial ended"}


    if now > st["last_seen"]:
        st["last_seen"] = now
        _save_state(data_dir, st, secret)
    return out


def activate(data_dir, secret: str, key: str, now: float | None = None) -> dict:




    now = time.time() if now is None else now
    payload, err = verify_key(secret, key, now)
    if err:
        return {"ok": False, "error": err}
    st = _load_state(data_dir)
    if st is None or not _state_valid(st, secret):
        st = _new_state(now)
    st["license"] = {"email": payload.get("email"), "tier": payload["tier"],
                     "exp": payload.get("exp") or 0, "iat": payload.get("iat"),
                     "key": key}
    st["last_seen"] = max(st["last_seen"], now)
    _save_state(data_dir, st, secret)
    return {"ok": True, "email": payload.get("email"), "tier": payload["tier"],
            "exp": payload.get("exp") or 0}


def grant(data_dir, secret, payload: dict, now: float | None = None, key: str | None = None) -> dict:







    now = time.time() if now is None else now
    tier = (payload or {}).get("tier")
    if tier not in TIERS:
        return {"ok": False, "error": f"unknown tier (нужен {'|'.join(TIERS)})"}
    try:
        exp = int((payload or {}).get("exp") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad exp"}
    if exp and now > exp:
        return {"ok": False, "error": "license expired"}
    st = _load_state(data_dir)
    if st is None or not _state_valid(st, secret):
        st = _new_state(now)
    st["license"] = {"email": payload.get("email"), "tier": tier,
                     "exp": exp, "iat": payload.get("iat"), "key": key}
    st["last_seen"] = max(st["last_seen"], now)
    _save_state(data_dir, st, secret)
    return {"ok": True, "email": payload.get("email"), "tier": tier, "exp": exp}


def set_revoked(data_dir, secret: str, revoked_at, now: float | None = None) -> bool:






    now = time.time() if now is None else now
    st = _load_state(data_dir)
    if st is None or not _state_valid(st, secret):
        return False
    lic = st.get("license")
    if not lic:
        return False
    if revoked_at:
        try:
            ts = float(revoked_at)
        except (TypeError, ValueError):
            return False
        if not ts > 0 or lic.get("revoked_at") == ts:
            return False
        lic["revoked_at"] = ts
    else:
        if "revoked_at" not in lic:
            return False
        lic.pop("revoked_at")
    _save_state(data_dir, st, secret)
    return True


def current_key(data_dir, secret: str) -> str | None:

    st = _load_state(data_dir)
    if st is None or not _state_valid(st, secret):
        return None
    lic = st.get("license") or {}
    return lic.get("key") or None


def allows_write(st: dict) -> bool:

    return st.get("state") in ("trial", "licensed")




FP_FILES = (
    "/sys/class/dmi/id/product_uuid",
    "/etc/machine-id",
    "/var/lib/dbus/machine-id",
)

_HOST_FP_RE = re.compile(r"^[0-9a-f]{64}$")


def machine_fingerprint(env=None) -> str | None:

















    env = os.environ if env is None else env
    host_fp = (env.get("HOST_FP") or "").strip().lower()
    if host_fp and _HOST_FP_RE.fullmatch(host_fp):
        return host_fp
    parts = []
    for p in FP_FILES:
        try:
            v = Path(p).read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if v:
            parts.append(v)
    try:
        mac = uuid.getnode()
        if (mac >> 40) & 1 == 0:
            parts.append(f"mac:{mac:012x}")
    except Exception:
        pass
    try:
        node = platform.node().strip()
        if node:
            parts.append(f"host:{node}")
    except Exception:
        pass
    if not parts:
        return None
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def anchor_trial_start(data_dir, secret: str, started_at, now=None) -> bool:






    now = time.time() if now is None else now
    try:
        ts = float(started_at)
    except (TypeError, ValueError):
        return False
    if not ts > 0:
        return False
    st = _load_state(data_dir)
    if st is not None and not _state_valid(st, secret):
        return False
    if st is None:
        st = _new_state(now)
    if ts >= st["first_seen"]:
        return False
    st["first_seen"] = ts
    _save_state(data_dir, st, secret)
    return True
