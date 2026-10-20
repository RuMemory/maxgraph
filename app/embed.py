#!/usr/bin/env python3



































































import hashlib
import json
import logging
import math
import os
import threading
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from operator import mul

log = logging.getLogger("maxgraph.embed")

DEFAULT_MODEL = "text-embedding-v3"
DEFAULT_TIMEOUT = 10
DEFAULT_REFRESH_TIMEOUT = 60

DEFAULT_REFRESH_SEC = 300
DEFAULT_DAILY_CAP = 200_000
DEFAULT_HYBRID_WEIGHT = 0.6

BATCH_SIZE = 32
ENTITY_TEXT_MAX = 1000
ENTITY_OBS_MAX = 3
QUERY_CACHE_MAX = 128
ROUND_DP = 6


class EmbedError(Exception):
    pass




def _int_env(value, default: int) -> int:




    try:
        v = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _float_env(value, default: float) -> float:


    try:
        v = float(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    if v != v:
        return default
    return min(1.0, max(0.0, v))








_runtime_lock = threading.Lock()
_PREFIX_MAX = 32


def _pfx(v) -> str:






    if not isinstance(v, str) or not 0 < len(v) <= _PREFIX_MAX:
        return ""
    try:
        v.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    if not v.isprintable() or not any(not c.isspace() for c in v):
        return ""
    return v


def model_fingerprint(cfg) -> str:




    cfg = cfg or {}
    model = str(cfg.get("model") or "")
    qp = str(cfg.get("query_prefix") or "")
    pp = str(cfg.get("passage_prefix") or "")
    if not qp and not pp:
        return model
    h = hashlib.sha256((qp + "\0" + pp).encode("utf-8")).hexdigest()[:8]
    return f"{model}|qp:{h}"


_runtime_cfg = None


def configure_runtime(url=None, key=None, model=None,
                      query_prefix=None, passage_prefix=None) -> None:














    global _runtime_cfg
    cfg = {"url": str(url or "").strip(), "key": str(key or "").strip(),
           "model": str(model or "").strip(),
           "query_prefix": _pfx(query_prefix),
           "passage_prefix": _pfx(passage_prefix)}
    if query_prefix and not cfg["query_prefix"] or passage_prefix and not cfg["passage_prefix"]:
        log.warning("embed: префикс из relay-конфига отклонён "
                    "(>32/не-printable/не-UTF-8) — игнорирую")
    with _runtime_lock:
        old = _runtime_cfg
        _runtime_cfg = cfg
    if old and (old["query_prefix"], old["passage_prefix"]) != \
            (cfg["query_prefix"], cfg["passage_prefix"]):
        log.info("embed: E5-префиксы сменились (query %r→%r, passage %r→%r) — "
                 "отпечаток хранилища изменится, корпус пересчитается",
                 old["query_prefix"], cfg["query_prefix"],
                 old["passage_prefix"], cfg["passage_prefix"])


def _apply_runtime(cfg: dict) -> dict:



    with _runtime_lock:
        rt = _runtime_cfg
    if rt is None:
        return cfg
    cfg = dict(cfg)
    cfg["url"], cfg["key"] = rt["url"], rt["key"]
    if rt["model"]:
        cfg["model"] = rt["model"]
    if rt["query_prefix"]:
        cfg["query_prefix"] = rt["query_prefix"]
    if rt["passage_prefix"]:
        cfg["passage_prefix"] = rt["passage_prefix"]
    return cfg


def config_from_env(env=None) -> dict:


    env = os.environ if env is None else env
    return _apply_runtime({
        "url": (env.get("EMBED_URL") or "").strip(),
        "key": (env.get("EMBED_KEY") or "").strip(),
        "model": (env.get("EMBED_MODEL") or "").strip() or DEFAULT_MODEL,
        "timeout": _int_env(env.get("EMBED_TIMEOUT"), DEFAULT_TIMEOUT),
        "refresh_timeout": _int_env(env.get("EMBED_REFRESH_TIMEOUT"),
                                    DEFAULT_REFRESH_TIMEOUT),
        "refresh_sec": _int_env(env.get("EMBED_REFRESH_SEC"), DEFAULT_REFRESH_SEC),
        "daily_cap": _int_env(env.get("EMBED_DAILY_CAP"), DEFAULT_DAILY_CAP),
        "hybrid_weight": _float_env(env.get("HYBRID_VECTOR_WEIGHT"),
                                    DEFAULT_HYBRID_WEIGHT),
        "query_prefix": _pfx(env.get("EMBED_QUERY_PREFIX")),
        "passage_prefix": _pfx(env.get("EMBED_PASSAGE_PREFIX")),
    })


def enabled(cfg=None) -> bool:

    cfg = config_from_env() if cfg is None else cfg
    on = bool(cfg.get("url") and cfg.get("key"))

    if on and not cfg["url"].lower().startswith("https://"):
        _warn_once("insecure-url", "embed: EMBED_URL не https — ключ пойдёт "
                                   "незашифрованным: %s", cfg["url"].split("://")[0] + "://…")
    return on


def should_start(cfg=None, readonly: bool = False) -> bool:



    return enabled(cfg) and not readonly






_warned = set()
_warn_lock = threading.Lock()


def _warn_once(key: str, msg: str, *args) -> None:
    with _warn_lock:
        if key in _warned:
            return
        _warned.add(key)
    log.warning(msg, *args)




def entity_text(entity) -> str:


    e = entity or {}
    parts = [str(e.get("name") or ""), str(e.get("title") or "")]
    obs = e.get("observations") or []
    parts.extend(str(o) for o in obs[:ENTITY_OBS_MAX])
    return "\n".join(p for p in parts if p)[:ENTITY_TEXT_MAX]


WINDOW_TEXT_MAX = 800
WINDOW_VECS_MAX = 4


def entity_windows(entity) -> "list[str]":




    main = entity_text(entity)
    e = entity or {}
    obs = [str(o) for o in (e.get("observations") or [])]
    rest = obs[ENTITY_OBS_MAX:]
    if not rest:
        return [main]
    windows = []
    cur = ""
    for fact in rest:
        cand = (cur + "\n" + fact) if cur else fact
        if len(cand) <= WINDOW_TEXT_MAX:
            cur = cand
        else:
            if cur:
                windows.append(cur)
            cur = fact[:WINDOW_TEXT_MAX]
        if len(windows) >= WINDOW_VECS_MAX:
            break
    if cur and len(windows) < WINDOW_VECS_MAX:
        windows.append(cur)
    return [main] + windows


def estimate_tokens(*texts: str) -> int:


    return max(1, sum(len(t or "") for t in texts) // 4)




class _NoRedirect(urllib.request.HTTPRedirectHandler):



    def redirect_request(self, *a, **k):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _http_post_json(url: str, headers: dict, payload: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:

    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _endpoint(url: str) -> str:


    u = url.rstrip("/")
    return u if u.endswith("/embeddings") else u + "/embeddings"


def embed_texts(texts, http_post=None, cfg=None):









    cfg = config_from_env() if cfg is None else cfg
    if not enabled(cfg):
        return None
    items = [str(t or "") for t in (texts or [])]
    if not items:
        return []
    post = http_post or _http_post_json
    payload = {"model": cfg["model"], "input": items}
    try:
        resp = post(_endpoint(cfg["url"]),
                    {"Authorization": f"Bearer {cfg['key']}"},
                    payload, cfg["timeout"])
    except Exception as e:
        _warn_once("request", "embed: запрос embeddings не удался: %s "
                   "— работаем без векторов (чистый FTS)", e)
        return None
    try:
        data = resp["data"]
        if not isinstance(data, list) or len(data) != len(items):
            raise ValueError(
                f"data: ожидалось {len(items)} векторов, получено "
                f"{len(data) if isinstance(data, list) else type(data).__name__}")
        if all(isinstance(d, dict) and isinstance(d.get("index"), int) for d in data):
            data = sorted(data, key=lambda d: d["index"])
        out = []
        for d in data:
            vec = d.get("embedding") if isinstance(d, dict) else None
            if (not isinstance(vec, list) or not vec
                    or any(not isinstance(x, (int, float)) or isinstance(x, bool)
                           for x in vec)):
                raise ValueError("embedding отсутствует или не список чисел")
            out.append([float(x) for x in vec])
    except (TypeError, KeyError, ValueError) as e:
        _warn_once("response", "embed: неожиданный ответ embeddings: %s "
                   "— работаем без векторов (чистый FTS)", e)
        return None
    return out




def normalize(vec) -> list:


    v = [float(x) for x in (vec or [])]
    n = math.sqrt(sum(x * x for x in v))
    if n == 0.0:
        return v
    return [x / n for x in v]


def dot(a, b) -> float:



    return sum(map(mul, a, b))


def cosine(a, b) -> float:


    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(x) * float(x) for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot(a, b) / (na * nb)


def cosine_scorer(store, query_vec):





    q = [float(x) for x in (query_vec or [])]
    qlen = len(q)
    data = store or {}

    def score(name):
        rec = data.get(name)
        if not rec:
            return None
        v = rec.get("v")
        if not isinstance(v, list) or len(v) != qlen or not qlen:
            return None
        best = dot(v, q)
        for w in rec.get("w") or []:
            if isinstance(w, list) and len(w) == qlen:
                d = dot(w, q)
                if d > best:
                    best = d
        return best

    return score
















_store_cache = {"path": None, "sig": None, "model": None, "data": {}}
_store_lock = threading.Lock()


def _file_sig(path: str):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load_store(path, model=None) -> dict:





    p = str(path)
    if model is None:
        model = model_fingerprint(config_from_env())
    sig = _file_sig(p)
    with _store_lock:
        if (_store_cache["path"] == p and _store_cache["sig"] == sig
                and _store_cache["model"] == model):
            return _store_cache["data"]
    data = {}
    if sig is not None:
        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            _warn_once("store-load", "embed: %s не читается — стартуем "
                       "с пустого хранилища (fail-open)", p)
            raw = None
        if isinstance(raw, dict):
            fp = raw.get("_model")
            for name, rec in raw.items():



                try:
                    if isinstance(rec, dict) and isinstance(rec.get("v"), list):
                        entry = {"v": [float(x) for x in rec["v"]],
                                 "u": str(rec.get("u") or "")}
                        w = rec.get("w")
                        if isinstance(w, list):
                            entry["w"] = [[float(x) for x in wv]
                                          for wv in w[:WINDOW_VECS_MAX]
                                          if isinstance(wv, list)]
                        data[str(name)] = entry
                except (TypeError, ValueError):
                    continue
            if fp != model:
                if data or fp is not None:
                    _warn_once("store-model", "embed: %s записан моделью %r, "
                               "текущая %r — вектора инвалидированы, "
                               "пополнитель пересчитает",
                               p, fp or "<без отпечатка>", model)
                data = {}
    with _store_lock:
        _store_cache.update({"path": p, "sig": sig, "model": model, "data": data})
    return data


def save_store(path, store, model=None) -> None:





    p = str(path)
    if model is None:
        model = model_fingerprint(config_from_env())
    model = str(model)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    out = {"_model": model}
    for name, rec in (store or {}).items():
        if str(name) == "_model":
            continue
        v = rec.get("v") if isinstance(rec, dict) else None
        if not isinstance(v, list):
            continue
        out[str(name)] = {"v": [round(float(x), ROUND_DP) for x in v],
                          "u": str(rec.get("u") or "")}
        w = rec.get("w")
        if isinstance(w, list) and w:
            out[str(name)]["w"] = [[round(float(x), ROUND_DP) for x in wv]
                                   for wv in w if isinstance(wv, list)]
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)
    with _store_lock:
        if _store_cache["path"] == p and _store_cache["data"] is store:
            _store_cache.update({"sig": _file_sig(p), "model": model})







_state_lock = threading.Lock()


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def load_state(path) -> dict:

    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
        if (isinstance(st, dict) and isinstance(st.get("tokens"), int)
                and isinstance(st.get("date"), str)):
            return st
    except Exception:
        pass
    return {"date": _utc_today(), "tokens": 0}


def save_state(path, state: dict) -> None:
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)


def usage_exceeded(path, cap: int, add: int = 0) -> bool:

    with _state_lock:
        st = load_state(path)
        if st["date"] != _utc_today():
            st = {"date": _utc_today(), "tokens": 0}
        return st["tokens"] + max(0, int(add)) > int(cap)


def spend(path, cost: int) -> None:

    with _state_lock:
        st = load_state(path)
        if st["date"] != _utc_today():
            st = {"date": _utc_today(), "tokens": 0}
        st["tokens"] += max(0, int(cost))
        save_state(path, st)




_query_cache = OrderedDict()
_query_lock = threading.Lock()


def query_vector(query, cfg=None, state_path=None, http_post=None):







    cfg = config_from_env() if cfg is None else cfg
    if not enabled(cfg):
        return None
    q = str(query or "").strip()
    if not q:
        return None
    q = str(cfg.get("query_prefix") or "") + q
    key = (q, model_fingerprint(cfg))
    with _query_lock:
        hit = _query_cache.get(key)
        if hit is not None:
            _query_cache.move_to_end(key)
            return hit
    if state_path is not None:
        cost = estimate_tokens(q)
        if usage_exceeded(state_path, cfg["daily_cap"], add=cost):
            _warn_once(f"cap:{_utc_today()}",
                       "embed: дневной кап токенов (%s) исчерпан — запрос "
                       "без вектора (чистый FTS до завтра)", cfg["daily_cap"])
            return None
        vecs = embed_texts([q], http_post=http_post, cfg=cfg)
        if not vecs:
            return None
        spend(state_path, cost)
    else:
        vecs = embed_texts([q], http_post=http_post, cfg=cfg)
        if not vecs:
            return None
    v = normalize(vecs[0])
    with _query_lock:
        _query_cache[key] = v
        _query_cache.move_to_end(key)
        while len(_query_cache) > QUERY_CACHE_MAX:
            _query_cache.popitem(last=False)
    return v




def refresh(entities, store_path, state_path, cfg=None, http_post=None) -> dict:














    cfg = _apply_runtime(config_from_env() if cfg is None else cfg)



    cfg = {**cfg,
           "timeout": cfg.get("refresh_timeout") or DEFAULT_REFRESH_TIMEOUT}
    stats = {"status": "ok", "pending": 0, "embedded": 0, "batches": 0,
             "tokens": 0, "error": None}
    if not enabled(cfg):
        stats["status"] = "disabled"
        return stats
    fp = model_fingerprint(cfg)
    pp = str(cfg.get("passage_prefix") or "")
    ents = [e for e in (entities or []) if isinstance(e, dict) and e.get("name")]
    names = {str(e["name"]) for e in ents}
    store = load_store(store_path, model=fp)
    orphans = [k for k in store if k not in names]
    for k in orphans:
        del store[k]
    stale = [e for e in ents
             if (store.get(str(e["name"])) or {}).get("u")
             != str(e.get("updatedAt") or "")]
    stats["pending"] = len(stale)
    changed = bool(orphans)
    for i in range(0, len(stale), BATCH_SIZE):
        batch = stale[i:i + BATCH_SIZE]
        win_lists = [entity_windows(e) for e in batch]
        texts = [pp + t for wl in win_lists for t in wl]
        cost = estimate_tokens(*texts)
        if usage_exceeded(state_path, cfg["daily_cap"], add=cost):
            stats["status"] = "capped"
            _warn_once(f"cap:{_utc_today()}",
                       "embed: дневной кап токенов (%s) исчерпан — "
                       "пополнение до завтра", cfg["daily_cap"])
            break


        vecs = []
        ok = True
        for j in range(0, len(texts), BATCH_SIZE):
            part = embed_texts(texts[j:j + BATCH_SIZE], http_post=http_post, cfg=cfg)
            if part is None:
                ok = False
                break
            vecs.extend(part)
        if not ok:
            stats["status"] = "error"
            stats["error"] = "embed_texts failed (см. warning выше)"
            break
        it = iter(vecs)
        for e, wl in zip(batch, win_lists):
            vs = [normalize(next(it)) for _ in wl]
            rec = {"v": vs[0], "u": str(e.get("updatedAt") or "")}
            if len(vs) > 1:
                rec["w"] = vs[1:]
            store[str(e["name"])] = rec
        spend(state_path, cost)
        stats["tokens"] += cost
        stats["batches"] += 1
        stats["embedded"] += len(batch)
        changed = True
    if changed:
        try:
            save_store(store_path, store, model=fp)
        except Exception as e:
            stats["status"] = "error"
            stats["error"] = f"save_store: {e}"
            _warn_once("store-save", "embed: не удалось записать %s: %s",
                       store_path, e)
    return stats
