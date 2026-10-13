#!/usr/bin/env python3
"""searchrank — ранжированный FTS-lite поверх графа памяти (Ф6, чистый stdlib).

Замена подстроки в op_search на BM25-подобный скоринг:

- tokenize(text): RU+EN, lowercase, ё→е, разбиение camelCase/kebab/snake,
  простой стемминг — срезание частых окончаний (компактные списки, без
  словарей-монстров; стеммер согласованный: одинаково режет запрос и документ).
- score(query_tokens, entity): BM25-подобный по полям с весами name ×3,
  title ×2, tags ×1.5, observations ×1; фразовый бонус (запрос из ≥2 токенов
  целиком в поле, по стемам); recency-бонус по updatedAt (≤+10%).
  Опциональные corpus-статистики (df/n_docs/avg_lens) подаёт rank(); без них
  idf=1 — score() остаётся юнит-тестируемым в одиночку.
- snippet(entity, query_tokens, width=160): лучший фрагмент observation →
  (text, highlights); highlights — [(start, end)] ПОЗИЦИИ совпавших токенов
  в text: никакой HTML-разметки, позиции безопасны для esc().
- rank(entities, query, limit, *, vector_weight=0.0, cosine_fn=None,
  actr_fn=None): топ-N с score>0, стабильная сортировка (score desc →
  updatedAt desc → name asc).
  В2.2 (опциональный гибрид): при vector_weight>0 и заданном cosine_fn(name)
  → cos|None итоговый score = (1−w)·fts_norm + w·cos_norm, где fts_norm =
  fts/max(fts) по кандидатам, cos_norm = (cos+1)/2 ∈ [0,1], отсутствующий
  вектор → нейтраль 0.5. Корпус ≤ HYBRID_FULL_SCAN_MAX → косинус по ВСЕМ
  кандидатам (full-scan: парафраз без лексического пересечения поднимается;
  fts=0 у непопавших, формула корректна); корпус больше → косинус только по
  FTS top-HYBRID_CANDIDATES (предфильтр: 10k×1024 dims полным проходом на
  чистом stdlib — ~секунды; 200×1024 ≈ миллисекунды; замеры — у констант).
  w≤0/без cosine_fn → byte-identical поведение 0.14.x (чистый FTS).
  В2.3 (опциональный ACT-R): при заданном actr_fn(name) → множитель ≥0
  итоговый score кандидата (FTS или blended) домножается на него ПОСЛЕ
  основной сортировки, затем пересортировка тем же стабильным ключом и срез
  [:limit] — часто читаемые сущности всплывают (модуль actr; максимум
  1+ACTR_MAX_BOOST). Сбой/NaN/≤0 → нейтраль 1.0 (fail-open, как cosine_fn).
  Без actr_fn → byte-identical поведение (ветка не исполняется).

Перф: токены сущности кэшируются по ключу (name, updatedAt) — updatedAt
обновляется при любой мутации, поэтому кэш самоинвалидируется. Тёплый поиск
по 10k сущностей — миллисекунды (benchmark в test_searchrank.py; гибрид —
в test_embed.py).
"""

import hashlib
import math
import re
import time
from collections import Counter
from datetime import datetime, timezone

# Веса полей (спека Ф6): name ×3, title ×2, tags ×1.5, observations ×1.
# Порядок dict = порядок убывания веса — используется для «один фразовый бонус
# по самому сильному полю».
FIELD_WEIGHTS = {"name": 3.0, "title": 2.0, "tags": 1.5, "observations": 1.0}

K1 = 1.5                      # BM25: насыщение tf
B = 0.75                      # BM25: нормировка по длине поля
RECENCY_MAX_BONUS = 0.10      # recency — лёгкий бонус: не более +10% к скору
RECENCY_DECAY_DAYS = 30.0     # exp-затухание: свежая сущность ≈ полный бонус
DEFAULT_LIMIT = 20
# В2.2: предфильтр гибрида — косинус считаем только по FTS top-N кандидатам
# (N ≥ limit). 10k сущностей × 1024 dims на чистом stdlib = ~0.5–1с полным
# проходом (numpy нельзя); 200×1024 ≈ 10–20мс — укладываемся в цель <150мс.
HYBRID_CANDIDATES = 200
# Full-scan: корпус ≤2000 кандидатов → косинус по ВСЕМ (включая ноль FTS-
# пересечений), иначе парафраз без общих токенов не поднимается НИКОГДА
# (recall-потолок предфильтра). Замер: 2000×1024 dims dot'ов чистым stdlib
# ≈ 35–40мс (без numpy) — приемлемо; 10k ≈ 180мс+FTS — за целью <150мс,
# поэтому большие корпусы остаются на предфильтре top-200 (~5–10мс).
HYBRID_FULL_SCAN_MAX = 2000

# ---------------------------------------------------------- токенизация -----

# Словарный прогон: буквы RU/EN + цифры; kebab/snake/пробелы/пунктуация — границы.
_WORD_RE = re.compile(r"[0-9a-zA-Zа-яА-ЯёЁ]+")
# camelCase внутри прогона: HTTPSConnection → HTTPS Connection, fooBar → foo Bar.
_CAMEL_RE = re.compile(
    r"[A-ZА-ЯЁ]+(?![a-zа-яё])|[A-ZА-ЯЁ][a-zа-яё]*|[a-zа-яё]+|[0-9]+")

# Частые окончания (компактно, без полных парадигм). Срезается ОДНО самое длинное
# совпадение при условии, что остаток ≥ _MIN_STEM. Согласованность важнее полноты:
# запрос и документ режутся одинаково, поэтому формы слова сходятся.
_EN_SUFFIXES = tuple(sorted((
    "ational", "tional", "tions", "tion", "ings", "ness", "ments", "ment",
    "able", "ible", "ally", "ous", "ive", "ful", "ies", "ied",
    "ing", "ers", "est", "ed", "er", "ly", "es", "s",
), key=len, reverse=True))

_RU_SUFFIXES = tuple(sorted((
    "иями", "ями", "ами", "иях", "ях", "ах",
    "ость", "ости", "ение", "ения", "ений", "ание", "ания",
    "овый", "евый",  # прилагательные: «поисковый» → «поиск»
    "ого", "его", "ому", "ему", "ыми", "ими",
    "ам", "ям", "ов", "ев", "ой", "ей", "ый", "ий", "ая", "яя",
    "ое", "ее", "ые", "ие", "ых", "их", "ым", "им", "ом", "ем",
    "а", "я", "о", "е", "ы", "и", "у", "ю", "ь",
), key=len, reverse=True))

_MIN_STEM = 3
_RU_FIRST = frozenset("абвгдежзийклмнопрстуфхцчшщъыьэюя")


def _subtokens(text):
    """(start, end, token_lower) по исходному тексту: словарные прогоны,
    внутри — camelCase-разбиение; lowercase, ё→е. Позиции — в символах text."""
    for m in _WORD_RE.finditer(text):
        base = m.start()
        for c in _CAMEL_RE.finditer(m.group(0)):
            yield base + c.start(), base + c.end(), \
                c.group(0).lower().replace("ё", "е")


def _stem(tok):
    """Один проход срезания окончания; цифры и коротыши (≤3) не трогаем."""
    if len(tok) <= _MIN_STEM or tok.isdigit():
        return tok
    suffixes = _RU_SUFFIXES if tok[0] in _RU_FIRST else _EN_SUFFIXES
    for suf in suffixes:
        if tok.endswith(suf) and len(tok) - len(suf) >= _MIN_STEM:
            return tok[: -len(suf)]
    return tok


def tokenize(text):
    """text → список стемов (str). Пустой/неподходящий ввод → []."""
    return [_stem(tok) for _, _, tok in _subtokens(str(text or ""))]


# ------------------------------------------------------------- кэш/поля -----

# (name, updatedAt, digest obs) → подготовленные поля. updatedAt меняется при
# любой мутации сущности → самоинвалидация; дайджест закрывает окно «поиск и
# инвалидация/фильтр в одну секунду» без коллизий (пара
# (len,sum) сталкивалась при равных длинах). Сироты вымываются потолком.
_prep_cache = {}
_CACHE_MAX = 20000


def _prepare(entity):
    """Кэшированная подготовка полей сущности: токены/Counter/длины по полям,
    set стемов (быстрый фильтр кандидатов), padded stems-тексты (фразы)."""
    obs = [str(o) for o in (entity.get("observations") or [])]
    obs_digest = hashlib.blake2b("\x00".join(obs).encode("utf-8", "ignore"),
                                 digest_size=8).digest()
    key = (str(entity.get("name") or ""), str(entity.get("updatedAt") or ""),
           obs_digest)
    prep = _prep_cache.get(key)
    if prep is not None:
        return prep
    if len(_prep_cache) >= _CACHE_MAX:
        _prep_cache.clear()
    texts = {
        "name": str(entity.get("name") or ""),
        "title": str(entity.get("title") or ""),
        "tags": " ".join(str(t) for t in (entity.get("tags") or [])),
        "observations": "\n".join(obs),
    }
    tf, lens, padded, stems = {}, {}, {}, set()
    for fname, text in texts.items():
        toks = tokenize(text)
        tf[fname] = Counter(toks)
        lens[fname] = len(toks)
        padded[fname] = " " + " ".join(toks) + " "  # границы токенов для фраз
        stems.update(toks)
    prep = {"tf": tf, "lens": lens, "padded": padded, "stems": stems, "obs": obs,
            "title": texts["title"], "name": texts["name"]}
    _prep_cache[key] = prep
    return prep


# --------------------------------------------------------------- скоринг ----

def _idf(tok, df, n_docs):
    """BM25-idf (Robertson); без корпусной статистики (юнит-режим) — 1.0."""
    if not df or not n_docs:
        return 1.0
    return math.log(1.0 + (n_docs - df.get(tok, 0) + 0.5) / (df.get(tok, 0) + 0.5))


def _recency_bonus(updated_at):
    """0.10 * exp(-age_days/30); битый/пустой updatedAt → 0 (штрафа нет)."""
    if not updated_at:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(updated_at))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (time.time() - dt.timestamp()) / 86400.0)
    except (ValueError, OverflowError, OSError):
        return 0.0
    return RECENCY_MAX_BONUS * math.exp(-age_days / RECENCY_DECAY_DAYS)


def score(query_tokens, entity, *, df=None, n_docs=0, avg_lens=None):
    """BM25-подобный скор сущности: Σ по токенам запроса и полям
    idf·(tf·(k1+1)/(tf + k1·(1−b+b·len/avg)))·вес поля, плюс фразовый бонус
    (≥2 токена запроса целиком в поле, по стемам — один раз, по самому сильному
    полю), в конце — recency-множитель (≤+10%). Нет совпадений → 0.0.
    df/n_docs/avg_lens подаёт rank(); без них idf=1, нормировка по длине самого
    поля (юнит-режим)."""
    toks = [t for t in query_tokens if t]
    if not toks:
        return 0.0
    prep = _prepare(entity)
    if prep["stems"].isdisjoint(toks):
        return 0.0
    idfs = [_idf(t, df, n_docs) for t in toks]
    total = 0.0
    for fname, weight in FIELD_WEIGHTS.items():
        counter = prep["tf"][fname]
        flen = prep["lens"][fname]
        if not flen:
            continue
        avg = (avg_lens or {}).get(fname) or flen
        norm = K1 * (1.0 - B + B * flen / avg)
        for tok, idf in zip(toks, idfs):
            tfv = counter.get(tok, 0)
            if tfv:
                total += weight * idf * (tfv * (K1 + 1.0)) / (tfv + norm)
    if total > 0.0 and len(toks) >= 2:
        phrase = " " + " ".join(toks) + " "
        avg_idf = sum(idfs) / len(idfs)
        for fname, weight in FIELD_WEIGHTS.items():  # по убыванию веса
            if phrase in prep["padded"][fname]:
                total += weight * avg_idf
                break
    return total * (1.0 + _recency_bonus(entity.get("updatedAt")))


def rank(entities, query, limit=DEFAULT_LIMIT, *, vector_weight=0.0, cosine_fn=None,
         actr_fn=None):
    """Топ-N сущностей по запросу → [(score, entity)], только score>0.
    Стабильная сортировка: score desc → updatedAt desc → name asc.
    Запрос без извлекаемых терминов → [] (фоллбек на подстроку — в op_search).

    В2.2 (гибрид, опционально): vector_weight w∈[0,1] + cosine_fn(name)
    → cos|None (модуль embed). Применяется ТОЛЬКО при w>0 и cosine_fn —
    иначе поведение byte-identical 0.14.x. Корпус ≤ HYBRID_FULL_SCAN_MAX →
    косинус по всем кандидатам (full-scan: fts=0 у непопавших — парафраз без
    общих токенов поднимается, в выдачу входит весь корпус, хвост = w·0.5);
    корпус больше → косинус по FTS top-HYBRID_CANDIDATES (предфильтр — перф,
    см. шапку). score кандидата: (1−w)·fts_norm + w·cos_norm; fts_norm =
    fts/max(fts) по кандидатам, cos_norm — mean-anchored по кандидатам запроса:
    (cos−mean)/(max−mean) обрезанное в [0,1] ((cos+1)/2 сжимало диапазон e5 —
    бенч Q4 в bench/); без вектора → 0.5 (нейтраль, не штраф). Сбой cosine_fn →
    тоже 0.5 (fail-open: векторный слой не должен ронять поиск).
    В гибридном режиме возвращаемый score — blended (0..1), не FTS.
    С actr_fn итог домножается на множитель до ACTR_MAX_BOOST →
    верхняя граница score ~1.25.

    В2.3 (ACT-R reinforcement, опционально): actr_fn(name) → множитель ≥0
    (модуль actr; нейтраль 1.0). Домножает итоговый скор каждого кандидата
    (FTS или blended) после основного скоринга, затем пересортировка и
    срез [:limit] — часто читаемое всплывает в пределах +ACTR_MAX_BOOST.
    Сбой/NaN/≤0/не-число → 1.0 (fail-open: ACT-R не роняет поиск). Без
    actr_fn ветка не исполняется — byte-identical 0.14.x/В2.2."""
    qts = tokenize(query)
    if not qts:
        return []
    ents = list(entities)
    if not ents:
        return []
    n = len(ents)
    # w вычисляем до раннего выхода: при full-scan пустой df не терминален
    # (векторное совпадение без единого общего токена — тоже выдача)
    w = 0.0
    if cosine_fn is not None:
        try:
            w = min(1.0, max(0.0, float(vector_weight)))
        except (TypeError, ValueError):
            w = 0.0
    preps = [(e, _prepare(e)) for e in ents]
    qset = set(qts)
    df = {}
    for _, prep in preps:
        for tok in qset & prep["stems"]:
            df[tok] = df.get(tok, 0) + 1
    if not df and not (w > 0.0 and n <= HYBRID_FULL_SCAN_MAX):
        return []
    sums = {f: 0 for f in FIELD_WEIGHTS}
    for _, prep in preps:
        for f in FIELD_WEIGHTS:
            sums[f] += prep["lens"][f]
    avg = {f: (sums[f] / n) or 1.0 for f in FIELD_WEIGHTS}
    scored = []
    for e, prep in preps:
        if prep["stems"].isdisjoint(qset):
            continue
        s = score(qts, e, df=df, n_docs=n, avg_lens=avg)
        if s > 0.0:
            scored.append((s, e))
    # python sort стабилен → многоуровневый ключ тремя проходами
    scored.sort(key=lambda se: se[1].get("name") or "")
    scored.sort(key=lambda se: se[1].get("updatedAt") or "", reverse=True)
    scored.sort(key=lambda se: se[0], reverse=True)
    try:
        lim = max(1, int(limit))
    except (TypeError, ValueError):
        lim = DEFAULT_LIMIT
    # --- В2.2: гибрид FTS+vector. Только при w>0 и cosine_fn; иначе —
    # чистый FTS, byte-identical 0.14.x. ---
    final = scored
    if w > 0.0 and (scored or n <= HYBRID_FULL_SCAN_MAX):
        # Full-scan (корпус ≤ HYBRID_FULL_SCAN_MAX): косинус по ВСЕМ сущностям
        # корпуса — парафраз без лексического пересечения иначе не поднимется
        # никогда (recall-потолок предфильтра = FTS top-HYBRID_CANDIDATES).
        # fts=0 у непопавших — бленд корректен (w·cos_norm); безвекторные →
        # нейтраль 0.5 из cosine_fn. Цена и порог — у HYBRID_FULL_SCAN_MAX.
        if n <= HYBRID_FULL_SCAN_MAX:
            fts_max = scored[0][0] if scored else 1.0  # scored отсортирован desc
            fts_by_name = {str(e.get("name") or ""): s for s, e in scored}
            cand = [(fts_by_name.get(str(e.get("name") or ""), 0.0), e)
                    for e in ents]
        else:
            cand = scored[:max(lim, HYBRID_CANDIDATES)]  # предфильтр: cos по топу
            fts_max = cand[0][0] or 1.0  # scored отсортирован desc; все fts > 0
        blended = []
        raw = []   # (fts, entity, cos|None) — cos собираем до бленда для z-norm
        coses = []
        for s, e in cand:
            try:
                cos = cosine_fn(str(e.get("name") or ""))
                cos = None if cos is None else float(cos)
            except Exception:  # fail-open: векторный слой не роняет поиск
                cos = None     # (включая не-числовой cos от кривого cosine_fn)
            if cos is not None:
                cos = min(1.0, max(-1.0, cos))
                coses.append(cos)
            raw.append((s, e, cos))
        # cos_norm — mean-anchored (v0.19.0+, по бенчу Q4): (cos+1)/2 сжимал
        # рабочий диапазон e5 (0.7..0.9) в 0.85..0.95 — векторный сигнал тонул
        # в fts_norm при w=0.35 (silent-hit 43%). Якорь — среднее косинусов
        # кандидатов запроса: выше среднего → (0..1], ниже → 0. Без вектора —
        # нейтраль 0.5 (не штраф); все косинусы равны → 0.5 (дискриминации нет).
        c_mean = sum(coses) / len(coses) if coses else 0.0
        c_span = (max(coses) - c_mean) if coses else 0.0
        for s, e, cos in raw:
            if cos is None:
                cos_norm = 0.5
            elif c_span > 0.0:
                cos_norm = min(1.0, max(0.0, (cos - c_mean) / c_span))
            else:
                cos_norm = 0.5
            blended.append(((1.0 - w) * (s / fts_max) + w * cos_norm, e))
        blended.sort(key=lambda se: se[1].get("name") or "")
        blended.sort(key=lambda se: se[1].get("updatedAt") or "", reverse=True)
        blended.sort(key=lambda se: se[0], reverse=True)
        final = blended
    # --- В2.3: ACT-R reinforcement. Только при actr_fn; иначе ветка не
    # исполняется (byte-identical). Множитель применяется к итоговому скору
    # (FTS или blended) ДО среза [:lim]: поднятый кандидат может войти в топ. ---
    if actr_fn is not None and final:
        boosted = []
        for s, e in final:
            try:
                m = float(actr_fn(str(e.get("name") or "")))
            except Exception:  # fail-open, как cosine_fn: ACT-R не роняет поиск
                m = 1.0
            if not math.isfinite(m) or m <= 0.0:  # NaN/inf/≤0 → нейтраль
                m = 1.0
            boosted.append((s * m, e))
        boosted.sort(key=lambda se: se[1].get("name") or "")
        boosted.sort(key=lambda se: se[1].get("updatedAt") or "", reverse=True)
        boosted.sort(key=lambda se: se[0], reverse=True)
        return boosted[:lim]
    return final[:lim]


# --------------------------------------------------------------- сниппет ----

def _window(text, anchor, width, qset):
    """Окно ≤width вокруг позиции anchor (не рвём слова) → (text, highlights).
    highlights — позиции токенов qset в итоговом text (учитывают «…»-префикс)."""
    width = max(20, int(width or 160))
    if len(text) <= width:
        start = 0
    else:
        start = max(0, anchor - width // 3)
        # левый край — не середина слова: недорезанный токен отбрасываем целиком
        # (start>0 обязателен: text[start-1] при start==0 — ПОСЛЕДНИЙ символ)
        while start > 0 and start < anchor \
                and text[start - 1].isalnum() and text[start].isalnum():
            start += 1
    end = min(len(text), start + width)
    # правый край — отступаем к началу недорезанного слова (но ≥20 символов тела)
    while end > start + 20 and end < len(text) \
            and text[end - 1].isalnum() and text[end].isalnum():
        end -= 1
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    body = text[start:end]
    off = len(prefix)
    hl = [(s + off, e + off) for s, e, tok in _subtokens(body) if _stem(tok) in qset]
    return prefix + body + suffix, hl


def snippet(entity, query_tokens, width=160):
    """Лучший фрагмент observation под запрос → (text, highlights).
    highlights — [(start, end)] позиции совпавших токенов в text (без HTML —
    безопасно для esc()). Матч вне observations (name/title/tags) → сниппет
    из title/имени с подсветкой. Пустой запрос → начало title/имени."""
    qset = {t for t in query_tokens if t}
    prep = _prepare(entity)
    best = None  # (hits, first_pos, obs_index); стабильно: первый с max hits
    if qset:
        for i, obs_text in enumerate(prep["obs"]):
            hits = 0
            first = -1
            for s, _e, tok in _subtokens(obs_text):
                if _stem(tok) in qset:
                    hits += 1
                    if first < 0:
                        first = s
            if hits and (best is None or hits > best[0]):
                best = (hits, first, i)
    if best is not None:
        return _window(prep["obs"][best[2]], best[1], width, qset)
    # совпадение было в name/title/tags — показываем title (или имя) с подсветкой
    return _window(prep["title"] or prep["name"], 0, width, qset)
