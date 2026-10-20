














import logging
import os
import threading
import time

log = logging.getLogger("maxgraph")

LEARN_WINDOW_SEC = 120
LEARN_BUFFER_CAP = 200
LEARN_TEXT_MAX = 200
LEARN_FLUSH_SEC = 300

_lock = threading.Lock()
_last_search = None
_buf = {}
_warned = set()


def _reset():

    global _last_search
    with _lock:
        _last_search = None
        _buf.clear()
        _warned.clear()


def _gated(env):


    env = os.environ if env is None else env
    return (env.get("STORE_MODE") or "").strip() == "1"


def _clean(text):


    return str(text or "").encode("utf-8", "ignore").decode("utf-8")[:LEARN_TEXT_MAX]


def record_search(query, hits, env=None, now=None):


    try:
        if _gated(env):
            return
        query = str(query or "")


        hits = {str(n): (str(t) if t else "") for n, t in (hits or [])}
        with _lock:
            global _last_search
            if not query.strip() or not hits:
                _last_search = None
            else:
                _last_search = (query, hits,
                                time.time() if now is None else now)
    except Exception:
        pass


def record_open(names, env=None, now=None):

    try:
        if _gated(env):
            return
        ts = time.time() if now is None else now
        with _lock:
            if not _last_search:
                return
            query, hits, qts = _last_search
            if ts - qts > LEARN_WINDOW_SEC:
                return
            for name in names or []:
                title = hits.get(str(name))
                if not title:
                    continue
                key = (_clean(query), _clean(title))
                if key in _buf:


                    _buf[key] = min(_buf[key] + 1, 1000)
                elif len(_buf) < LEARN_BUFFER_CAP:
                    _buf[key] = 1

    except Exception:
        pass


def snapshot():

    with _lock:
        return [{"q": q, "t": t, "c": c} for (q, t), c in _buf.items()]


def flush(post=None, base_url=None, env=None):




    global _warned
    try:
        if _gated(env) or not (base_url or "").strip():
            return False
        events = snapshot()
        if not events:
            return False
        if post is None:
            from urllib import request as _ur
            import json as _json

            def post(url, payload, timeout=5):
                req = _ur.Request(url, data=_json.dumps(payload).encode("utf-8"),
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
                try:
                    raw = _ur.urlopen(req, timeout=timeout).read(65536)
                    return _json.loads(raw.decode("utf-8"))
                except Exception:
                    return None
        resp = post(base_url.rstrip("/") + "/api/telemetry/learn",
                    {"events": events}, timeout=5)
        if isinstance(resp, dict) and resp.get("ok"):
            with _lock:
                for ev in events:
                    key = (ev["q"], ev["t"])
                    if key in _buf:
                        left = _buf[key] - ev["c"]
                        if left > 0:
                            _buf[key] = left
                        else:
                            del _buf[key]
                _warned.discard("flush")
            return True
        raise ValueError("not ok: %r" % (resp,))
    except Exception as e:
        with _lock:
            if "flush" not in _warned:
                _warned.add("flush")
                log.warning("learn: flush не удался (буфер сохранён, %d событий): %r",
                            len(_buf), e)
        return False


def start_learn_loop(post=None, base_url=None, env=None, interval=LEARN_FLUSH_SEC,
                     stop=None):


    stop = stop or threading.Event()

    def _loop():
        while not stop.is_set():
            flush(post=post, base_url=base_url, env=env)
            stop.wait(interval)

    t = threading.Thread(target=_loop, daemon=True, name="learn-events")
    t.start()
    return t
