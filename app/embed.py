#!/usr/bin/env python3
"""
Maxgraph embed — опциональный семантический слой поиска (В2.2, WAVE2-PLAN).
Чистый stdlib (urllib) — модуль тестируется отдельно от
сервера (инъекция http_post); app.py подключает его к op_search и фоновому
пополнителю.

Механика:
  фоновый пополнитель (embed_loop в app.py) → entity_windows: основной текст
  (name+title+3 факта, ≤1000 chars) + до 4 доп. окон фактов ≤800 chars
  (v0.20.2 — на EMBED_URL уходит ≤4200 симв/сущность) → под-вызовы ≤32 текстов
  → OpenAI-compatible POST /embeddings → вектора
  нормализуются (|v|=1 → косинус = dot product) → data/embeddings.json
  ({"_model": отпечаток модели, name: {"v": [...], "u": updatedAt}}, атомарная
  запись tmp+replace; файл с чужим/без отпечатка → вектора инвалидированы,
  пополнитель пересчитает). Поиск (app._hybrid_search): запрос эмбеддится
  (LRU 128 по (query, model)), косинусы — по FTS top-200 кандидатам, а при
  корпусе ≤ HYBRID_FULL_SCAN_MAX — по всем (full-scan, recall парафраз;
  см. searchrank.rank), гибрид (1−w)·fts_norm + w·cos_norm — в searchrank.

Офлайн-safe: без EMBED_URL/EMBED_KEY модуль выключен (enabled() → False) —
поведение byte-identical 0.14.x (чистый FTS). Fail-open: любая ошибка
(сеть, HTTP, битый ответ, битый файл) → None / пустое хранилище, log.warning
ОДИН раз на сессию по каждому классу сбоев (без спама). События в пульс
НЕ пишутся (иначе фоновый тик каждые 5 минут засорял бы timeline).

Env (compose подставляет пустые строки — поэтому `or default`, не get(default)):
  EMBED_URL             — OpenAI-compatible endpoint (base или полный
                          …/embeddings), напр. https://dashscope…/v1
  EMBED_KEY             — API-ключ
  EMBED_MODEL           — модель (default text-embedding-v3)
  EMBED_TIMEOUT         — сек на один embeddings-запрос (default 10)
  EMBED_REFRESH_SEC     — период фонового пополнителя (default 300)
  EMBED_REFRESH_TIMEOUT — сек на один вызов пополнителя (default 60; батчи
                          тяжелее поисковых запросов, v0.25.1)
  EMBED_DAILY_CAP       — кап токенов/UTC-день (default 200000; оценка len/4)
  EMBED_QUERY_PREFIX    — префикс поискового запроса (E5: «query: »; default "")
  EMBED_PASSAGE_PREFIX  — префикс текстов сущностей (E5: «passage: »; default "")
  HYBRID_VECTOR_WEIGHT  — вес векторной составляющей w в гибриде
                          (default 0.6 — по бенчу Q4, клэмпится в [0,1];
                          0 → чистый FTS)

Префиксы — конвенция векторизуемого текста (E5 обучен на «query: »/«passage: »),
а НЕ хардкод: дефолтная модель generic (text-embedding-v3), где префиксы вредны.
Задаются env или приезжают в relay-конфиге; отпечаток хранилища при непустых
префиксах получает маркер «model|qp:<hash8>» — смена конвенции инвалидирует
вектора тем же механизмом, что смена модели (один плановый пересчёт). Без
префиксов отпечаток — голая модель (bit-compat, self-hosted не инвалидируется).

Zero-config: configure_runtime(url, key, model) задаёт конфиг поверх env без
рестарта процесса (relay-конфиг после активации лицензии); приоритет НАД env
до рестарта. Поисковый путь и refresh подхватывают на следующем вызове/тике.

Лимиты/компромиссы (задокументированный выбор):
  - Предфильтр: косинус считается только по FTS top-200 (searchrank
    .HYBRID_CANDIDATES). 10k×1024 dims на чистом stdlib — ~0.5–1с полным
    проходом (нет numpy); 200×1024 ≈ 10–20мс. Цена: сущность без единого
    FTS-совпадения векторно не поднимется (recall-стадия = FTS).
  - Сущность без вектора (ещё не посчитан) → cos_norm = 0.5 (нейтраль,
    не штраф): порядок внутри FTS-выдачи без векторов не меняется.
  - Вектора в памяти — plain list[float] (10k×1024 ≈ 300MB RSS): фича
    опциональна и включается только явным EMBED_URL/KEY.
  - Запрос эмбеддится синхронно на поисковом пути (как _load() в op_search):
    LRU 128 снимает повторы, EMBED_TIMEOUT ограничивает худший случай.
  - Кап примерный: check-then-spend без транзакции между потоками (гонка
    ≤ одного батча/запроса — для len/4-оценки допустимо).
"""

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
DEFAULT_TIMEOUT = 10           # сек на один /embeddings вызов (поисковый путь)
DEFAULT_REFRESH_TIMEOUT = 60   # сек на вызов пополнителя: батч 32 реальных окон
                               # (~18K симв.) на 2-ядерном CPU ≈ 9с (v0.25.1)
DEFAULT_REFRESH_SEC = 300      # период фонового пополнителя
DEFAULT_DAILY_CAP = 200_000    # токенов/UTC-день (оценка len/4)
DEFAULT_HYBRID_WEIGHT = 0.6   # w в (1−w)·fts_norm + w·cos_norm; 0.6 — по бенчу
                              # Q4 (bench/): меньше — парафразы тонут в fts_norm
BATCH_SIZE = 32                # потолок текстов в одном /embeddings вызове
ENTITY_TEXT_MAX = 1000         # потолок символов текста сущности
ENTITY_OBS_MAX = 3             # сколько первых фактов идёт в текст сущности
QUERY_CACHE_MAX = 128          # LRU кэш векторов запросов (по (запрос, отпечаток))
ROUND_DP = 6                   # округление компонент при записи (±5e-7 ≪ шума)


class EmbedError(Exception):
    """Fail-open ошибка embed-слоя (конфигурация/сеть/ответ): ничего не падает."""


# ----------------------------------------------------------------- config ----

def _int_env(value, default: int) -> int:
    """int из env-строки; пусто/мусор/≤0 → default (compose даёт пустые строки).
    Неположительное значение ломало бы семантику молча:
    timeout≤0 → urlopen ValueError; refresh≤0 → time.sleep убивает поток;
    cap≤0 → вечный «capped» без единого вызова."""
    try:
        v = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _float_env(value, default: float) -> float:
    """float из env-строки; пусто/мусор/NaN → default; результат клэмпится
    в [0,1] (вес гибрида вне диапазона бессмысленен)."""
    try:
        v = float(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return min(1.0, max(0.0, v))


# -------------------------------------------------------- runtime override ---
# Zero-config: relay-конфиг после активации лицензии — без рестарта процесса.
# Атомарная замена неизменяемого dict под локом (стиль _store/_query ниже):
# читатели (config_from_env в поисковом пути, merge в тике refresh) всегда
# видят цельный конфиг. None → правит env (поведение до первого вызова).

_runtime_lock = threading.Lock()
_PREFIX_MAX = 32               # кап длины префикса из env/relay (защита от мусора)


def _pfx(v) -> str:
    """Префикс из env/relay-конфига: только str ≤32 символов, иначе «нет поля».
    НЕ стрипаем: пробел в конце «query: » — значимая часть E5-конвенции.
    SECURITY (ревизия 0.25.0): отсекаем не-кодируемое в UTF-8 (lone surrogate
    легален в JSON — "\\ud800", но .encode() на нём падает → иначе вредоносный
    relay убивал бы ВЕСЬ поиск клиента до рестарта — обход fail-open),
    не-printable (управляющие молча портят вектора; \\0 ломал бы инъективность
    сепаратора отпечатка) и whitespace-only («весь из пробелов» — мусор)."""
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
    """Отпечаток хранилища: голая модель, а при заданных префиксах —
    «model|qp:<hash8>». Префиксы меняют векторизуемый текст так же, как смена
    модели → та же инвалидация (один плановый пересчёт), новый механизм не
    нужен. Без префиксов — bit-compat со старым отпечатком."""
    cfg = cfg or {}
    model = str(cfg.get("model") or "")
    qp = str(cfg.get("query_prefix") or "")
    pp = str(cfg.get("passage_prefix") or "")
    if not qp and not pp:
        return model
    h = hashlib.sha256((qp + "\0" + pp).encode("utf-8")).hexdigest()[:8]
    return f"{model}|qp:{h}"


_runtime_cfg = None  # None → env; dict {"url", "key", "model", …} → приоритет над env


def configure_runtime(url=None, key=None, model=None,
                      query_prefix=None, passage_prefix=None) -> None:
    """Runtime-конфиг embed поверх env (zero-config, без рестарта процесса).

    Непустые url+key → включён (enabled() → True) даже без EMBED_* в env;
    url=None или key=None → выключен (enabled() → False) даже при заданном env.
    Приоритет НАД env до рестарта процесса (override в памяти; назад к env —
    только рестарт). model=None/пусто → не переопределяет EMBED_MODEL.
    query_prefix/passage_prefix — E5-конвенция текстов из relay-конфига:
    невалидное (не str / пусто / >32) → не переопределяет env (present-only,
    сбросить env-префикс через relay нельзя). Потокобезопасно (лок + цельная
    замена). Поиск (app._hybrid_search читает config_from_env на каждый вызов)
    и refresh (merge override в каждый тик) подхватывают без рестарта;
    пополнитель, НЕ запущенный при старте (embed был выключен), из этого
    модуля не стартует — запуск потока на стороне app.py.
    """
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
    """Runtime-override поверх cfg (configure_runtime); override=None → как было.
    Возвращает НОВЫЙ dict — переданный cfg не мутируется (его может держать
    embed_loop со стартовыми значениями для refresh_sec)."""
    with _runtime_lock:
        rt = _runtime_cfg
    if rt is None:
        return cfg
    cfg = dict(cfg)
    cfg["url"], cfg["key"] = rt["url"], rt["key"]
    if rt["model"]:  # model не задан в override → остаётся env/дефолт
        cfg["model"] = rt["model"]
    if rt["query_prefix"]:  # префиксы present-only: нет поля → env как был
        cfg["query_prefix"] = rt["query_prefix"]
    if rt["passage_prefix"]:
        cfg["passage_prefix"] = rt["passage_prefix"]
    return cfg


def config_from_env(env=None) -> dict:
    """Конфиг из env (или переданного mapping — для тестов). Runtime-override
    (configure_runtime) приоритетнее env и живёт до рестарта процесса."""
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
    """Embed включён только с URL И ключом; без них — молча выключен (офлайн)."""
    cfg = config_from_env() if cfg is None else cfg
    on = bool(cfg.get("url") and cfg.get("key"))
    # LOW-1 (security-review волны 2): не-https URL — Bearer уйдёт открытым текстом.
    if on and not cfg["url"].lower().startswith("https://"):
        _warn_once("insecure-url", "embed: EMBED_URL не https — ключ пойдёт "
                                   "незашифрованным: %s", cfg["url"].split("://")[0] + "://…")
    return on


def should_start(cfg=None, readonly: bool = False) -> bool:
    """Запускать ли фоновый пополнитель: enabled и НЕ READONLY-зеркало
    (зеркало не пишет; гибридный ПОИСК на зеркале при этом работает — он
    только читает embeddings.json + эмбеддит запрос)."""
    return enabled(cfg) and not readonly


# --------------------------------------------------------------- warnings ----
# Fail-open без спама: один warning на класс сбоя за сессию (процесс). Кап —
# один за UTC-день (ключ «cap:<date>»). Тесты сбрасывают _warned.clear().

_warned = set()
_warn_lock = threading.Lock()


def _warn_once(key: str, msg: str, *args) -> None:
    with _warn_lock:
        if key in _warned:
            return
        _warned.add(key)
    log.warning(msg, *args)


# ------------------------------------------------------------------ texts ----

def entity_text(entity) -> str:
    """Текст сущности для эмбеддинга: name + title + первые 3 факта,
    ≤1000 символов (гигиена входа + предсказуемая стоимость токенов)."""
    e = entity or {}
    parts = [str(e.get("name") or ""), str(e.get("title") or "")]
    obs = e.get("observations") or []
    parts.extend(str(o) for o in obs[:ENTITY_OBS_MAX])
    return "\n".join(p for p in parts if p)[:ENTITY_TEXT_MAX]


WINDOW_TEXT_MAX = 800      # окно доп. фактов для вектора (e5-small: 512 токенов)
WINDOW_VECS_MAX = 4        # ≤4 доп. вектора на мега-сущность (потолок стоимости)


def entity_windows(entity) -> "list[str]":
    """[основной текст] + доп. окна фактов, НЕ влезших в основной (v0.20.2,
    мега-сущности): факты пакуются целиком в окна ≤WINDOW_TEXT_MAX, окон
    ≤WINDOW_VECS_MAX. Малые сущности → ровно [entity_text] (поведение как
    раньше). Скорер берёт max-косинус по окнам → релевантный факт не тонет
    за пределами первых трёх наблюдений (bench: промахи мега-сущностей)."""
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
    """Оценка токенов len/4 (провайдер usage у embeddings-ответа может
    отсутствовать/отличаться, считаем сами)."""
    return max(1, sum(len(t or "") for t in texts) // 4)


# -------------------------------------------------------------------- LLM ----

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """30x → HTTPError: urllib иначе тащит Authorization Bearer на redirect-target
    (утечка relay-токена, SECURITY v0.19.0)."""

    def redirect_request(self, *a, **k):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _http_post_json(url: str, headers: dict, payload: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """POST json → dict. Точка инъекции для тестов — параметр http_post."""
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _endpoint(url: str) -> str:
    """EMBED_URL → /embeddings URL: полный путь принимаем как есть,
    base (…/v1, хост relay) — дополняем."""
    u = url.rstrip("/")
    return u if u.endswith("/embeddings") else u + "/embeddings"


def embed_texts(texts, http_post=None, cfg=None):
    """OpenAI-compatible POST /embeddings → list[list[float]] | None.

    Fail-open: не сконфигурирован / сеть / HTTP / битый JSON / неверная форма
    (data не список нужной длины, embedding не список чисел) → None +
    один warning на сессию. Пустой вход → [] без сети. Порядок ответа
    восстанавливается по полю index, если оно есть у всех элементов.
    All-or-nothing: любой битый вектор обнуляет весь батч (частичная запись
    дала бы рассинхрон u=updatedAt с реальным содержимым).
    """
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


# ------------------------------------------------------------------ math -----

def normalize(vec) -> list:
    """Вектор → единичная длина (|v|=1). Нулевой вектор возвращается как есть
    (dot с ним = 0 → cos 0 → нейтральная 0.5 в гибриде — не падаем)."""
    v = [float(x) for x in (vec or [])]
    n = math.sqrt(sum(x * x for x in v))
    if n == 0.0:
        return v
    return [x / n for x in v]


def dot(a, b) -> float:
    """Скалярное произведение (map/mul — самый быстрый чистый stdlib).
    Для заранее нормализованных векторов это и есть косинус. Длины должны
    совпадать — проверка на стороне cosine_scorer."""
    return sum(map(mul, a, b))


def cosine(a, b) -> float:
    """Полная косинусная близость (для НЕнормализованных векторов; в горячем
    пути используется dot по нормализованным). Нулевой вектор → 0.0."""
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(x) * float(x) for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot(a, b) / (na * nb)


def cosine_scorer(store, query_vec):
    """Замыкание name → cos|None для searchrank.rank(cosine_fn=…).
    Вектора в store уже нормализованы при сохранении, query_vec нормализуется
    в query_vector → косинус = dot. Нет вектора / рассинхрон размерностей
    (сменили EMBED_MODEL — старые вектора пересчитает пополнитель) → None
    (гибрид трактует как нейтраль 0.5)."""
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
        for w in rec.get("w") or []:  # v0.20.2: доп. окна мега-сущностей — max
            if isinstance(w, list) and len(w) == qlen:
                d = dot(w, q)
                if d > best:
                    best = d
        return best

    return score


# ----------------------------------------------------------------- store -----
# data/embeddings.json: {"_model": отпечаток модели, name: {"v": [float,…],
# "u": updatedAt}}. Отпечаток нужен потому, что refresh сверяет только
# updatedAt: смена EMBED_MODEL сделала бы чужие вектора вечным мусором.
# Файл с чужим/отсутствующим (старый формат) "_model" читается как ПУСТОЙ
# (один warning на сессию): косинус-скорер даёт нейтраль 0.5, пополнитель
# пересчитывает всё и пишет новый отпечаток. Запись атомарная (tmp +
# os.replace, как _save в app.py): падение посреди оставляет прежний файл
# целым. In-memory кэш по (path, mtime_ns, size, model): поисковый путь не
# парсит JSON на каждый запрос; save_store обновляет сигнатуру кэша, когда
# сохранялся именно закэшированный объект (пополнитель мутирует его in-place).
# В памяти хранилище — чистый {name: rec} (без "_model"); "_model" — имя
# зарезервировано (kebab-id с «_» не используем).

_store_cache = {"path": None, "sig": None, "model": None, "data": {}}
_store_lock = threading.Lock()


def _file_sig(path: str):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load_store(path, model=None) -> dict:
    """embeddings.json → {name: {"v": [...], "u": str}} (кэш по mtime+size+модель).
    Отсутствует/битый/чужая форма → {} (fail-open, один warning на сессию).
    model — отпечаток текущей конфигурации (None → model_fingerprint(config)): файл
    с чужим или отсутствующим (старый формат) отпечатком → {} + один warning
    (вектора «отсутствуют»: скорер → нейтраль 0.5, refresh всё пересчитает)."""
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
            fp = raw.get("_model")  # отпечаток модели (None — старый формат)
            for name, rec in raw.items():
                # битая запись пропускается, а не роняет всё хранилище
                # (иначе один мусорный вектор = полный пересчёт по капу);
                # "_model" — не dict с "v" → отсекается тем же фильтром
                try:
                    if isinstance(rec, dict) and isinstance(rec.get("v"), list):
                        entry = {"v": [float(x) for x in rec["v"]],
                                 "u": str(rec.get("u") or "")}
                        w = rec.get("w")  # v0.20.2: доп. окна мега-сущностей
                        if isinstance(w, list):
                            entry["w"] = [[float(x) for x in wv]
                                          for wv in w[:WINDOW_VECS_MAX]
                                          if isinstance(wv, list)]
                        data[str(name)] = entry
                except (TypeError, ValueError):
                    continue
            if fp != model:
                if data or fp is not None:  # инвалидировать нечего → тихо
                    _warn_once("store-model", "embed: %s записан моделью %r, "
                               "текущая %r — вектора инвалидированы, "
                               "пополнитель пересчитает",
                               p, fp or "<без отпечатка>", model)
                data = {}
    with _store_lock:
        _store_cache.update({"path": p, "sig": sig, "model": model, "data": data})
    return data


def save_store(path, store, model=None) -> None:
    """Атомарная запись хранилища (tmp + replace). Компоненты округляются до
    6 знаков — погрешность косинуса ≤5e-4, файл вдвое меньше. Ошибки ввода-
    вывода летят наверх (refresh ловит, fail-open); частично записанный tmp
    не затрагивает прежний файл. Записи без list-вектора пропускаются.
    Сверху пишется отпечаток "_model" (None → model_fingerprint(config))."""
    p = str(path)
    if model is None:
        model = model_fingerprint(config_from_env())
    model = str(model)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    out = {"_model": model}
    for name, rec in (store or {}).items():
        if str(name) == "_model":  # зарезервировано под отпечаток: запись перезаписала
            continue               # бы fingerprint → self-DoS (SECURITY v0.19.0)
        v = rec.get("v") if isinstance(rec, dict) else None
        if not isinstance(v, list):
            continue
        out[str(name)] = {"v": [round(float(x), ROUND_DP) for x in v],
                          "u": str(rec.get("u") or "")}
        w = rec.get("w")  # v0.20.2: доп. окна мега-сущностей
        if isinstance(w, list) and w:
            out[str(name)]["w"] = [[round(float(x), ROUND_DP) for x in wv]
                                   for wv in w if isinstance(wv, list)]
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)
    with _store_lock:  # файл теперь == кэшированный объект: не парсить заново
        if _store_cache["path"] == p and _store_cache["data"] is store:
            _store_cache.update({"sig": _file_sig(p), "model": model})


# ------------------------------------------------------------------- state ---
# data/embed.state.json: {"date": <UTC-день>, "tokens": n} — дневной кап
# (примерный, len/4). Check-then-spend под одним локом; гонка между потоком
# пополнителя и поиском ≤ одного батча/запроса — для примерного капа ок.

_state_lock = threading.Lock()


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def load_state(path) -> dict:
    """embed.state.json → {"date", "tokens"}; отсутствует/битый → свежий день."""
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
    """True, если tokens+add превысит дневной кап (день роллится по UTC)."""
    with _state_lock:
        st = load_state(path)
        if st["date"] != _utc_today():
            st = {"date": _utc_today(), "tokens": 0}
        return st["tokens"] + max(0, int(add)) > int(cap)


def spend(path, cost: int) -> None:
    """Списать cost токенов с дневного счётчика (атомарная запись)."""
    with _state_lock:
        st = load_state(path)
        if st["date"] != _utc_today():
            st = {"date": _utc_today(), "tokens": 0}
        st["tokens"] += max(0, int(cost))
        save_state(path, st)


# ----------------------------------------------------------- query vectors ---

_query_cache = OrderedDict()  # (префиксованный запрос, отпечаток) → норм. вектор
_query_lock = threading.Lock()


def query_vector(query, cfg=None, state_path=None, http_post=None):
    """Вектор поискового запроса (нормализованный) или None (fail-open).

    LRU 128 по (query, model): повторные поиски бесплатны. Промах = один
    /embeddings вызов; токены (len/4) идут в дневной кап, если задан
    state_path (превышение → None → чистый FTS, один warning в день).
    Неудачи НЕ кэшируются — следующий запрос честно повторит попытку.
    """
    cfg = config_from_env() if cfg is None else cfg
    if not enabled(cfg):
        return None
    q = str(query or "").strip()
    if not q:
        return None
    q = str(cfg.get("query_prefix") or "") + q  # E5: «query: »; пусто у generic
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


# ----------------------------------------------------------------- refresh ---

def refresh(entities, store_path, state_path, cfg=None, http_post=None) -> dict:
    """Один тик фонового пополнителя: досчитать вектора сущностей без
    актуального (store[name]["u"] != updatedAt), батчами ≤BATCH_SIZE.

    → {"status": ok|capped|error|disabled, "pending", "embedded", "batches",
       "tokens", "error"}. Fail-open: сбой сети → status=error, уже
    посчитанные батчи сохраняются; кап → status=capped до завтра (один
    warning в день). Сироты (сущность удалена из графа) выметаются из
    хранилища. Хранилище пишется ОДИН раз в конце тика (атомарно) — не
    O(n²) перезаписей на начальном заполнении. Пульс не пишется (без спама).
    Runtime-override (configure_runtime) мержится в cfg на каждый тик —
    embed_loop, запущенный со стартовым cfg, подхватывает zero-config без
    рестарта; хранилище с чужим отпечатком (модель/префиксы) читается пустым →
    весь корпус пересчитывается под новую конфигурацию.
    """
    cfg = _apply_runtime(config_from_env() if cfg is None else cfg)
    # Поисковый таймаут (10с) тесен для refresh-батчей: 32 реальных окна
    # (~18K симв.) на 2-ядерном CPU ≈ 9с → read timeout, миграция стоит.
    # У пополнителя свой таймаут; поисковый путь не тронут (v0.25.1).
    cfg = {**cfg,
           "timeout": cfg.get("refresh_timeout") or DEFAULT_REFRESH_TIMEOUT}
    stats = {"status": "ok", "pending": 0, "embedded": 0, "batches": 0,
             "tokens": 0, "error": None}
    if not enabled(cfg):
        stats["status"] = "disabled"
        return stats
    fp = model_fingerprint(cfg)
    pp = str(cfg.get("passage_prefix") or "")  # E5: «passage: »; пусто у generic
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
        win_lists = [entity_windows(e) for e in batch]  # v0.20.2: окна мега-сущностей
        texts = [pp + t for wl in win_lists for t in wl]  # «»+t == t: без веток
        cost = estimate_tokens(*texts)
        if usage_exceeded(state_path, cfg["daily_cap"], add=cost):
            stats["status"] = "capped"
            _warn_once(f"cap:{_utc_today()}",
                       "embed: дневной кап токенов (%s) исчерпан — "
                       "пополнение до завтра", cfg["daily_cap"])
            break
        # Развёрнутые окна могут превышать потолок вызова (BATCH_SIZE текстов;
        # у сервиса MAX_BATCH=64) — режем на под-вызовы, маппинг по порядку.
        vecs = []
        ok = True
        for j in range(0, len(texts), BATCH_SIZE):
            part = embed_texts(texts[j:j + BATCH_SIZE], http_post=http_post, cfg=cfg)
            if part is None:  # fail-open: сеть/ответ — тик кончается, цикл жив
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
                rec["w"] = vs[1:]  # доп. окна — scorer берёт max-косинус
            store[str(e["name"])] = rec
        spend(state_path, cost)
        stats["tokens"] += cost
        stats["batches"] += 1
        stats["embedded"] += len(batch)
        changed = True
    if changed:
        try:
            save_store(store_path, store, model=fp)
        except Exception as e:  # OSError диска, ValueError округления — fail-open
            stats["status"] = "error"
            stats["error"] = f"save_store: {e}"
            _warn_once("store-save", "embed: не удалось записать %s: %s",
                       store_path, e)
    return stats
