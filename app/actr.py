#!/usr/bin/env python3
"""
Maxgraph actr — ACT-R reinforcement: частые факты всплывают (В2.3, WAVE2-PLAN).
Чистый stdlib — модуль тестируется отдельно от сервера; app.py инжектит
boost_fn в searchrank.rank (actr_fn, как cosine_fn из embed) и в сортировки
секций session_start.

Идея (у claude-mem — ACT-R base-level activation): чем чаще и недавнее агент
обращался к сущности, тем выше она в выдаче — память сама учится, что важно.
Классическая формула B = ln(Σ t_i^-d) требует истории всех обращений; храним
только агрегат {n, last_ts} и упрощаем:
    boost           = ln(1 + n) · recency_factor
    recency_factor  = 0.5 ** (days_since_last_access / HALF_LIFE_DAYS=14)
    множитель ранга = 1 + min(boost, 1.0) · ACTR_MAX_BOOST (default 0.25 → ≤+25%)
Нейтраль: n=0 → ln(1)=0 → множитель РОВНО 1.0 (ранг не меняется ни на эпсилон).

Счётчики НЕ в memory.json: иначе каждый search = fsync-шторм, а правка
updatedAt сущности сломала бы recency-скоринг и кэш searchrank (ключ кэша —
(name, updatedAt)). Вместо этого in-memory dict {name: {"n", "t"}} + атомарный
flush (tmp+replace) в data/access.json раз в ACTR_FLUSH_SEC (default 300с,
daemon-поток — как embed) и при shutdown (atexit в app.main()).
Загрузка при старте; отсутствующий/битый файл → с нуля (fail-open, один
warning на сессию). События в пульс НЕ пишутся (фоновый тик засорял бы
timeline); события flush тоже нет.

Потокобезопасность _touch: обновление счётчика — read-modify-write
(rec["n"] += 1 компилируется в несколько байткодов, GIL НЕ делает r-m-w
атомарным → гонка теряла бы инкременты; регрессионный тест: 10 потоков ×
1000 касаний обязаны дать ровно 10000). Поэтому один leaf-уровневый
threading.Lock на батч имён. Лок «листовой»: внутри нет I/O, сети и захвата
других локов (_lock/_codes_lock) → дедлоки исключены по построению. Один
захват на батч (≤20 имён у search) — микросекунды; «lock-free надёжда на GIL»
здесь была бы молчаливой потерей данных.

Изоляция проектов сохраняется: счётчики глобальны по имени, но имена сущностей
глобально уникальны (name — первичный ключ графа, create_entities делает merge
по имени), поэтому счётчик «чужого» проекта не подмешивается в чужой корпус;
буст применяется ПОСЛЕ фильтра корпуса (Ф1/Ф3) и только к видимым именам —
утечки через буст нет (худший случай: имя получает свой же честно накопленный
множитель). Сироты (сущность удалена из графа) из счётчиков не выметаются:
запись — байты на имя, а повторно созданная сущность с тем же именем наследует
историю обращений (то же имя = та же тема — осознанный компромисс).

Env (compose подставляет пустые строки — поэтому `or default`, не get(default)):
  ACTR_FLUSH_SEC        — период flush счётчиков в access.json (default 300)
  ACTR_MAX_BOOST        — потолок буста (default 0.25 = +25%; 0 → буст выключен:
                          rank вызывается без actr_fn, поведение byte-identical)
  ACTR_HALF_LIFE_DAYS   — полураспад recency-фактора в днях (default 14)
"""

import json
import logging
import math
import os
import threading
import time

log = logging.getLogger("maxgraph.actr")

DEFAULT_FLUSH_SEC = 300        # период фонового flush (как embed refresh)
DEFAULT_MAX_BOOST = 0.25       # потолок: максимум +25% к рангу
DEFAULT_HALF_LIFE_DAYS = 14.0  # recency_factor = 0.5 ** (days / 14)
TOUCH_TOP = 20                 # search учитывает top-20 выдачи (не весь корпус)


# ----------------------------------------------------------------- config ----

def _int_env(value, default: int) -> int:
    """int из env-строки; пусто/мусор/≤0 → default (compose даёт пустые строки).
    flush_sec≤0 убил бы поток (time.sleep) или превратил flush в флуд."""
    try:
        v = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _unit_float_env(value, default: float) -> float:
    """float из env, клэмп [0,1] (для ACTR_MAX_BOOST: 0 — валидное «выключено»,
    >100% буста бессмысленны); пусто/мусор/NaN → default."""
    try:
        v = float(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return min(1.0, max(0.0, v))


def _pos_float_env(value, default: float) -> float:
    """float из env, строго >0 (для ACTR_HALF_LIFE_DAYS: 0 → деление на ноль,
    отрицательный полураспад — мусорный смысл); пусто/мусор/NaN → default."""
    try:
        v = float(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    if v != v or v <= 0.0 or not math.isfinite(v):  # NaN/≤0/inf
        return default
    return v


def config_from_env(env=None) -> dict:
    """Конфиг из env (или переданного mapping — для тестов)."""
    env = os.environ if env is None else env
    return {
        "flush_sec": _int_env(env.get("ACTR_FLUSH_SEC"), DEFAULT_FLUSH_SEC),
        "max_boost": _unit_float_env(env.get("ACTR_MAX_BOOST"), DEFAULT_MAX_BOOST),
        "half_life_days": _pos_float_env(env.get("ACTR_HALF_LIFE_DAYS"),
                                         DEFAULT_HALF_LIFE_DAYS),
    }


# --------------------------------------------------------------- warnings ----
# Fail-open без спама: один warning на класс сбоя за сессию (как embed).

_warned = set()
_warn_lock = threading.Lock()


def _warn_once(key: str, msg: str, *args) -> None:
    with _warn_lock:
        if key in _warned:
            return
        _warned.add(key)
    log.warning(msg, *args)


# ---------------------------------------------------------------- formula ----

def boost(n, last_ts, now=None, *, half_life_days=DEFAULT_HALF_LIFE_DAYS) -> float:
    """ACT-R буст по агрегату: ln(1+n) · 0.5**(age_days/half_life).
    n≤0 → ровно 0.0 (нейтраль). last_ts в будущем/битый → age 0 (без штрафа)."""
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
    """Итоговый множитель ранга: 1 + min(boost, 1.0) · max_boost ∈ [1, 1+max_boost].
    n=0 → ровно 1.0 (IEEE-точно: 1.0 + 0.0·x == 1.0) — byte-identical поведение."""
    b = min(boost(n, last_ts, now=now, half_life_days=half_life_days), 1.0)
    try:
        mb = float(max_boost)
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_BOOST
    if not math.isfinite(mb) or mb <= 0.0:
        return 1.0 + b * 0.0  # выключено: всегда ровно 1.0
    return 1.0 + b * min(1.0, mb)


def boost_fn(store, cfg=None):
    """Замыкание name → множитель ∈ [1, 1+max_boost] для searchrank.rank(actr_fn=…)
    и сортировок секций session_start. Нейтраль: неизвестное имя (n=0) → 1.0."""
    cfg = config_from_env() if cfg is None else cfg
    mb, hl = cfg["max_boost"], cfg["half_life_days"]

    def mult(name):
        n, t = store.get(name)
        return multiplier(n, t, max_boost=mb, half_life_days=hl)

    return mult


# ------------------------------------------------------------------ store ----
# data/access.json: {name: {"n": int, "t": float}}. Запись атомарная
# (tmp + os.replace, как _save/embed.save_store): падение посреди оставляет
# прежний файл целым.

def _load_file(path) -> dict:
    """access.json → {name: {"n","t"}}; нет файла → {}, битый/чужая форма → {}
    (fail-open, один warning). Битая ЗАПИСЬ пропускается, не роняя остальные."""
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
    """Атомарная запись счётчиков. Fail-open: ошибка → False + один warning
    (flush — фоновая обязанность, она не должна ронять сервер/atexit)."""
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
    """In-memory счётчики обращений {name: {"n": int, "t": float}}.

    Потокобезопасность: leaf-лок на read-modify-write (см. шапку модуля);
    снимок для flush делается под тем же локом, сама запись файла — ВНЕ лока
    (I/O под локом растянуло бы критическую секцию на дисковую задержку)."""

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def touch(self, names, now=None) -> int:
        """+1 обращение и t=now для каждого имени (батч под одним захватом
        лока — O(1) на имя, без I/O/сети). Пустые/не-строковые имена
        пропускаются. → сколько имён учтено."""
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
        """(n, t) по имени; неизвестное → (0, 0.0) — нейтраль для формулы."""
        with self._lock:
            rec = self._data.get(str(name or ""))
            return (0, 0.0) if rec is None else (rec["n"], rec["t"])

    def clear(self) -> None:
        """Полный сброс (тесты; осознанный «забыть обращения»)."""
        with self._lock:
            self._data.clear()

    def load(self, path) -> int:
        """Загрузить счётчики из access.json (заменяет in-memory). → число записей."""
        data = _load_file(path)
        with self._lock:
            self._data = data
        return len(data)

    def flush(self, path) -> bool:
        """Атомарно записать снимок счётчиков (tmp+replace). Fail-open → bool."""
        with self._lock:
            snap = {k: dict(v) for k, v in self._data.items()}
        return _save_file(path, snap)
