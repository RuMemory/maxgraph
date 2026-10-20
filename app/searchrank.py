#!/usr/bin/env python3








































import hashlib
import math
import re
import time
from collections import Counter
from datetime import datetime, timezone




FIELD_WEIGHTS = {"name": 3.0, "title": 2.0, "tags": 1.5, "observations": 1.0}

K1 = 1.5
B = 0.75
RECENCY_MAX_BONUS = 0.10
RECENCY_DECAY_DAYS = 30.0
DEFAULT_LIMIT = 20



HYBRID_CANDIDATES = 200





HYBRID_FULL_SCAN_MAX = 2000




_WORD_RE = re.compile(r"[0-9a-zA-Zа-яА-ЯёЁ]+")

_CAMEL_RE = re.compile(
    r"[A-ZА-ЯЁ]+(?![a-zа-яё])|[A-ZА-ЯЁ][a-zа-яё]*|[a-zа-яё]+|[0-9]+")




_EN_SUFFIXES = tuple(sorted((
    "ational", "tional", "tions", "tion", "ings", "ness", "ments", "ment",
    "able", "ible", "ally", "ous", "ive", "ful", "ies", "ied",
    "ing", "ers", "est", "ed", "er", "ly", "es", "s",
), key=len, reverse=True))

_RU_SUFFIXES = tuple(sorted((
    "иями", "ями", "ами", "иях", "ях", "ах",
    "ость", "ости", "ение", "ения", "ений", "ание", "ания",
    "овый", "евый",
    "ого", "его", "ому", "ему", "ыми", "ими",
    "ам", "ям", "ов", "ев", "ой", "ей", "ый", "ий", "ая", "яя",
    "ое", "ее", "ые", "ие", "ых", "их", "ым", "им", "ом", "ем",
    "а", "я", "о", "е", "ы", "и", "у", "ю", "ь",
), key=len, reverse=True))

_MIN_STEM = 3
_RU_FIRST = frozenset("абвгдежзийклмнопрстуфхцчшщъыьэюя")


def _subtokens(text):


    for m in _WORD_RE.finditer(text):
        base = m.start()
        for c in _CAMEL_RE.finditer(m.group(0)):
            yield base + c.start(), base + c.end(), \
                c.group(0).lower().replace("ё", "е")


def _stem(tok):

    if len(tok) <= _MIN_STEM or tok.isdigit():
        return tok
    suffixes = _RU_SUFFIXES if tok[0] in _RU_FIRST else _EN_SUFFIXES
    for suf in suffixes:
        if tok.endswith(suf) and len(tok) - len(suf) >= _MIN_STEM:
            return tok[: -len(suf)]
    return tok


def tokenize(text):

    return [_stem(tok) for _, _, tok in _subtokens(str(text or ""))]








_prep_cache = {}
_CACHE_MAX = 20000


def _prepare(entity):


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
        padded[fname] = " " + " ".join(toks) + " "
        stems.update(toks)
    prep = {"tf": tf, "lens": lens, "padded": padded, "stems": stems, "obs": obs,
            "title": texts["title"], "name": texts["name"]}
    _prep_cache[key] = prep
    return prep




def _idf(tok, df, n_docs):

    if not df or not n_docs:
        return 1.0
    return math.log(1.0 + (n_docs - df.get(tok, 0) + 0.5) / (df.get(tok, 0) + 0.5))


def _recency_bonus(updated_at, now=None):




    if not updated_at:
        return 0.0
    if now is None:
        now = time.time()
    try:
        dt = datetime.fromisoformat(str(updated_at))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - dt.timestamp()) / 86400.0)
    except (ValueError, OverflowError, OSError):
        return 0.0
    return RECENCY_MAX_BONUS * math.exp(-age_days / RECENCY_DECAY_DAYS)


def score(query_tokens, entity, *, df=None, n_docs=0, avg_lens=None, now=None):







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
        for fname, weight in FIELD_WEIGHTS.items():
            if phrase in prep["padded"][fname]:
                total += weight * avg_idf
                break
    return total * (1.0 + _recency_bonus(entity.get("updatedAt"), now))


def rank(entities, query, limit=DEFAULT_LIMIT, *, vector_weight=0.0, cosine_fn=None,
         actr_fn=None):

























    qts = tokenize(query)
    if not qts:
        return []
    ents = list(entities)
    if not ents:
        return []
    n = len(ents)


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
    now = time.time()
    for e, prep in preps:
        if prep["stems"].isdisjoint(qset):
            continue
        s = score(qts, e, df=df, n_docs=n, avg_lens=avg, now=now)
        if s > 0.0:
            scored.append((s, e))

    scored.sort(key=lambda se: se[1].get("name") or "")
    scored.sort(key=lambda se: se[1].get("updatedAt") or "", reverse=True)
    scored.sort(key=lambda se: se[0], reverse=True)
    try:
        lim = max(1, int(limit))
    except (TypeError, ValueError):
        lim = DEFAULT_LIMIT


    final = scored
    if w > 0.0 and (scored or n <= HYBRID_FULL_SCAN_MAX):





        if n <= HYBRID_FULL_SCAN_MAX:
            fts_max = scored[0][0] if scored else 1.0
            fts_by_name = {str(e.get("name") or ""): s for s, e in scored}
            cand = [(fts_by_name.get(str(e.get("name") or ""), 0.0), e)
                    for e in ents]
        else:
            cand = scored[:max(lim, HYBRID_CANDIDATES)]
            fts_max = cand[0][0] or 1.0
        blended = []
        raw = []
        coses = []
        for s, e in cand:
            try:
                cos = cosine_fn(str(e.get("name") or ""))
                cos = None if cos is None else float(cos)
            except Exception:
                cos = None
            if cos is not None:
                cos = min(1.0, max(-1.0, cos))
                coses.append(cos)
            raw.append((s, e, cos))





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



    if actr_fn is not None and final:
        boosted = []
        for s, e in final:
            try:
                m = float(actr_fn(str(e.get("name") or "")))
            except Exception:
                m = 1.0
            if not math.isfinite(m) or m <= 0.0:
                m = 1.0
            boosted.append((s * m, e))
        boosted.sort(key=lambda se: se[1].get("name") or "")
        boosted.sort(key=lambda se: se[1].get("updatedAt") or "", reverse=True)
        boosted.sort(key=lambda se: se[0], reverse=True)
        return boosted[:lim]
    return final[:lim]




def _window(text, anchor, width, qset):


    width = max(20, int(width or 160))
    if len(text) <= width:
        start = 0
    else:
        start = max(0, anchor - width // 3)


        while start > 0 and start < anchor \
                and text[start - 1].isalnum() and text[start].isalnum():
            start += 1
    end = min(len(text), start + width)

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




    qset = {t for t in query_tokens if t}
    prep = _prepare(entity)
    best = None
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

    return _window(prep["title"] or prep["name"], 0, width, qset)
