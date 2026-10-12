"""rankcfg (W3, issue #22): версионированный словарь синонимов из relay-конфига.

Магазин агрегирует пары «стем запроса → стем заголовка» из анонимных
learn-events и раздаёт словарь в /api/relay/config (поле "rank"). Клиент
применяет его как расширение FTS-запроса (только FTS — векторный запрос и
веса не трогаем, решение владельца W3-Q1/Q3).

Гарантии (спека §6):
- валидация всего сетевого/дискового ввода; невалид → отклонить целиком,
  остаться на last-good (data/rank_config.json);
- атомарные записи (tmp+replace); битый файл → пустой словарь, не падение;
- пустой словарь → expand_query identity (бит-в-бит прежнее поведение);
- синоним синонима не разворачивается (один шаг).

Словарь: ключи — СТЕМЫ токенов запроса (searchrank.tokenize; морфология-
устойчивый lookup), значения — поверхностные формы (читаемо в /admin;
re-tokenize при расширении возвращает тот же стем — стеммер идемпотентен
на своих выходах, проверено 2026-10-12).
"""
import json
import os
import threading

import searchrank

TOKEN_MAX = 32               # символов на токен/синоним
PAIRS_CAP = 500              # пар в словаре максимум
SYNONYMS_PER_TOKEN_CAP = 5   # синонимов на токен максимум (топ по count на сервере)

_lock = threading.Lock()
_state = {"version": 0, "synonyms": {}}


def _reset():
    """Тесты: полный сброс module-state."""
    global _state
    with _lock:
        _state = {"version": 0, "synonyms": {}}


def current():
    """Текущий конфиг (копия)."""
    with _lock:
        return {"version": _state["version"], "synonyms": dict(_state["synonyms"])}


def _valid_token(tok):
    """Стем из сети/файла: непустой, ≤32, без whitespace/управляющих, кодируется
    в utf-8 (lone surrogate ломал бы запись last-good — урок HIGH-1 v0.25.0)."""
    if not isinstance(tok, str) or not tok or len(tok) > TOKEN_MAX:
        return False
    try:
        tok.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(ch.isspace() or ord(ch) < 32 for ch in tok)


def _validate(cfg):
    """dict сети/файла → нормализованный {version, synonyms} или None.
    Отклонение ЦЕЛИКОМ (атомарность): частично валидный конфиг не применяется.
    Список синонимов > капа — не повод для отказа, режем до капа."""
    if not isinstance(cfg, dict):
        return None
    version = cfg.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        return None
    syn = cfg.get("synonyms")
    if not isinstance(syn, dict) or len(syn) > PAIRS_CAP:
        return None
    out = {}
    for tok, syns in syn.items():
        if not _valid_token(tok) or not isinstance(syns, list) or not syns:
            return None
        clean = []
        for s in syns:
            if not _valid_token(s):
                return None
            clean.append(s)
        out[tok] = clean[:SYNONYMS_PER_TOKEN_CAP]
    return {"version": version, "synonyms": out}


def _persist(path):
    tmp = None
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def apply_rank_config(cfg, path=None):
    """Применить конфиг из relay-ответа. True — применён (и персистнут при path).
    False — отклонён: невалиден или version не новее текущей; состояние цело."""
    norm = _validate(cfg)
    if norm is None:
        return False
    with _lock:
        global _state
        if norm["version"] <= _state["version"]:
            return False
        _state = norm
        if path is not None:
            _persist(path)
        return True


def load(path):
    """Last-good при старте: битый/отсутствующий файл → состояние не трогаем."""
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return apply_rank_config(cfg, path=None)  # версия из файла может быть старше


def expand_query(query):
    """FTS-расширение: к запросу дописываются стемы-синонимы его стемов.
    Один шаг (без multi-hop). Пустой словарь → запрос бит-в-бит прежний."""
    query = str(query or "")
    with _lock:
        syn = _state["synonyms"]
        if not syn:
            return query
        extra = []
        for stem in searchrank.tokenize(query):
            extra.extend(s for s in syn.get(stem, ()) if s not in extra)
    return query + " " + " ".join(extra) if extra else query
