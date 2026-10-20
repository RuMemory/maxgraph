


















import json
import os
import threading

import searchrank

TOKEN_MAX = 32
PAIRS_CAP = 500
SYNONYMS_PER_TOKEN_CAP = 5

_lock = threading.Lock()
_state = {"version": 0, "synonyms": {}}


def _reset():

    global _state
    with _lock:
        _state = {"version": 0, "synonyms": {}}


def current():

    with _lock:
        return {"version": _state["version"], "synonyms": dict(_state["synonyms"])}


def _valid_token(tok):


    if not isinstance(tok, str) or not tok or len(tok) > TOKEN_MAX:
        return False
    try:
        tok.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(ch.isspace() or ord(ch) < 32 for ch in tok)


def _validate(cfg):



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

    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return apply_rank_config(cfg, path=None)


def expand_query(query):


    query = str(query or "")
    with _lock:
        syn = _state["synonyms"]
        if not syn:
            return query
        extra = []
        for stem in searchrank.tokenize(query):
            extra.extend(s for s in syn.get(stem, ()) if s not in extra)
    return query + " " + " ".join(extra) if extra else query
