"""learn-events (W2, issue #21): анонимные события «запрос → открытый заголовок».

Клиентская половина трубы обучения (SPEC-learn-events.md): op_search запоминает
выдачу, op_open в окне LEARN_WINDOW_SEC порождает событие (q, title, счётчик),
daemon флашит буфер пакетом на магазин POST /api/telemetry/learn.

Гарантии (спека §6):
- анонимность: без license key/install_id/fp/IP/project/name — только тексты
  запроса и заголовок (≤LEARN_TEXT_MAX code points);
- STORE_MODE / пустой base_url → полный no-op (v0.41.0: выключатель TELEMETRY=0
  удалён решением владельца — телеметрия всегда ON);
- сеть ТОЛЬКО в daemon-потоке (flush); record_* — память под локом, ноль latency
  в поиске;
- fail-open: любое исключение внутри гасится, поиск/открытие не могут упасть
  из-за learn-events.
"""
import logging
import os
import threading
import time

log = logging.getLogger("maxgraph")

LEARN_WINDOW_SEC = 120    # окно связки search→open; дальше — ложные пары
LEARN_BUFFER_CAP = 200    # потолок буфера; новые пары дропаются (повторы считаются)
LEARN_TEXT_MAX = 200      # truncation q/t в code points
LEARN_FLUSH_SEC = 300     # период daemon-флаша

_lock = threading.Lock()
_last_search = None       # (query:str, hits:{name:title}, ts:float) | None
_buf = {}                 # (q, t) -> c
_warned = set()           # warn_once-ключи (урок W1: тишина ≠ зависание)


def _reset():
    """Тесты: полный сброс module-state."""
    global _last_search
    with _lock:
        _last_search = None
        _buf.clear()
        _warned.clear()


def _gated(env):
    """True = выключено. Гейт: STORE_MODE (магазин сам себе не шлёт).
    v0.41.0: TELEMETRY=0 удалён — оптаута больше нет."""
    env = os.environ if env is None else env
    return (env.get("STORE_MODE") or "").strip() == "1"


def _clean(text):
    """Truncation по code points + отсек lone surrogates
    (иначе utf-8 encode упадёт на каждом флаше)."""
    return str(text or "").encode("utf-8", "ignore").decode("utf-8")[:LEARN_TEXT_MAX]


def record_search(query, hits, env=None, now=None):
    """Запомнить выдачу как источник связки. hits = [(name, title), ...].
    Пустой query/выдача → связка сбрасывается. Никакой сети."""
    try:
        if _gated(env):
            return
        query = str(query or "")
        # title пуст → "" — фоллбэка на name НЕТ (name —
        # внутренний слуг, в событие не уходит никогда; record_open пропустит).
        hits = {str(n): (str(t) if t else "") for n, t in (hits or [])}
        with _lock:
            global _last_search
            if not query.strip() or not hits:
                _last_search = None
            else:
                _last_search = (query, hits,
                                time.time() if now is None else now)
    except Exception:  # fail-open: learn-events не может уронить op_search
        pass


def record_open(names, env=None, now=None):
    """open по имени из свежей выдачи → событие (q, title) с дедуп-счётчиком."""
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
                if not title:  # нет в выдаче или без title → события нет
                    continue
                key = (_clean(query), _clean(title))
                if key in _buf:
                    # кап счётчика — c>1000 сервер отклонит,
                    # иначе пара стала бы poison-pill с вечным ретраем
                    _buf[key] = min(_buf[key] + 1, 1000)
                elif len(_buf) < LEARN_BUFFER_CAP:
                    _buf[key] = 1
                # переполнение: новые пары молча дропаются (спека §4.2)
    except Exception:
        pass


def snapshot():
    """Текущий буфер как список событий контракта [{q,t,c}] (копия)."""
    with _lock:
        return [{"q": q, "t": t, "c": c} for (q, t), c in _buf.items()]


def flush(post=None, base_url=None, env=None):
    """Отправить буфер пакетом на магазин. True — буфер очищен (или пуст).

    Сбой сети/не-ok → буфер СОХРАНЯЕТСЯ до следующего тика, один warning на
    warn_once-ключ (урок W1: тишина ≠ зависание); успех сбрасывает ключ."""
    global _warned
    try:
        if _gated(env) or not (base_url or "").strip():
            return False
        events = snapshot()
        if not events:
            return False  # пустой буфер — сети нет вообще
        if post is None:
            from urllib import request as _ur  # локальный импорт: stdlib only
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
                for ev in events:  # вычесть только отправленное (могли добавить)
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
    """Daemon-флаш по образцу embed_loop: тик сразу, потом каждые interval сек.
    Гейты внутри flush; поток стартует всегда, но при гейтах — вечный сон."""
    stop = stop or threading.Event()

    def _loop():
        while not stop.is_set():
            flush(post=post, base_url=base_url, env=env)
            stop.wait(interval)

    t = threading.Thread(target=_loop, daemon=True, name="learn-events")
    t.start()
    return t
