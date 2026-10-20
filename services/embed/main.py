






import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

MODEL_NAME = "intfloat/multilingual-e5-small"


MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
MODEL_ONNX_SHA256 = "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665"
DAILY_TOKEN_LIMIT = 200_000
MAX_BODY_BYTES = 128 * 1024
MAX_BATCH = 64
MAX_TEXT_CHARS = 8000

log = logging.getLogger("embed-relay")

SECRET = os.environ.get("RELAY_HMAC_SECRET", "")
if not SECRET:
    raise SystemExit("RELAY_HMAC_SECRET is not set")

DATA_DIR = Path(os.environ.get("EMBED_DATA_DIR", "data"))
USAGE_FILE = DATA_DIR / "usage.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)






def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s.encode() + b"=" * (-len(s) % 4))


def verify_token(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig = parts
    try:
        expected = hmac.new(SECRET.encode(), f"relay:{payload_b64}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("lk"), str):
        return None
    if not isinstance(payload.get("exp"), (int, float)) or payload["exp"] <= time.time():
        return None
    return payload




_usage_lock = threading.Lock()
_usage: dict = {}


def _load_usage(path: Path, store: dict) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    for day, rec in data.items():
        if isinstance(day, str) and isinstance(rec, dict):
            clean = {str(k): int(v) for k, v in rec.items()
                     if isinstance(v, (int, float)) and not isinstance(v, bool)}
            if clean:
                store[day] = clean


def _consume(store: dict, path: Path, lk: str, tokens: int,
             limit: int | None, charge: bool = True) -> bool:



    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _usage_lock:
        for day in [d for d in store if d != today]:
            del store[day]
        day_usage = store.setdefault(today, {})
        used = day_usage.get(lk, 0)
        if limit is not None and used + tokens > limit:
            return False
        if charge:
            day_usage[lk] = used + tokens
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(store), encoding="utf-8")
            os.replace(tmp, path)
        return True


def consume_quota(lk: str, tokens: int) -> bool:

    return _consume(_usage, USAGE_FILE, lk, tokens, DAILY_TOKEN_LIMIT)





_distinct_lock = threading.Lock()
_distinct = {"day": "", "lks": set()}
DISTINCT_LK_ALERT_AT = 50


def _note_lk(lk: str) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _distinct_lock:
        if _distinct["day"] != today:
            _distinct["day"] = today
            _distinct["lks"] = set()
        lks = _distinct["lks"]
        if lk in lks:
            return
        lks.add(lk)
        n = len(lks)
    if n == DISTINCT_LK_ALERT_AT:
        log.warning("embed: %d разных lk за UTC-день — возможен фарм "
                    "триальных токенов", n)


_load_usage(USAGE_FILE, _usage)




_model = None
_model_lock = threading.Lock()


_embed_lock = threading.Lock()


def _verify_model_file() -> None:


    cache = Path(os.environ.get("FASTEMBED_CACHE_PATH") or
                 (Path.home() / ".cache" / "fastembed"))
    hits = [p for p in cache.rglob("model.onnx") if "multilingual-e5-small" in str(p)]
    digest = None
    if hits:
        h = hashlib.sha256()
        with hits[0].open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
    if digest != MODEL_ONNX_SHA256:
        raise SystemExit(f"model sha256 mismatch: {digest} != {MODEL_ONNX_SHA256} "
                         f"(ревизия {MODEL_REVISION}) — отказываемся стартовать")


def get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from fastembed import TextEmbedding
                from fastembed.common.model_description import ModelSource, PoolingType



                TextEmbedding.add_custom_model(
                    model=MODEL_NAME,
                    pooling=PoolingType.MEAN,
                    normalization=True,
                    sources=ModelSource(hf=MODEL_NAME),
                    dim=384,
                )
                _model = TextEmbedding(MODEL_NAME)
                _verify_model_file()
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    model = get_model()
    with _embed_lock:
        return [vec.tolist() for vec in model.embed(texts)]




def _err(status: int, code: str, **extra) -> JSONResponse:
    return JSONResponse({"error": code, **extra}, status_code=status)


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": MODEL_NAME}


@app.post("/embeddings")
async def embeddings(request: Request):
    auth = request.headers.get("authorization", "")
    payload = verify_token(auth[7:]) if auth.startswith("Bearer ") else None
    if payload is None:
        return _err(401, "unauthorized")
    _note_lk(payload["lk"])


    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BODY_BYTES:
        return _err(413, "payload_too_large")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return _err(413, "payload_too_large")
    try:
        req = json.loads(body)
    except ValueError:
        return _err(400, "invalid_json")

    raw_input = req.get("input") if isinstance(req, dict) else None
    texts = [raw_input] if isinstance(raw_input, str) else raw_input
    if not isinstance(texts, list) or not texts or len(texts) > MAX_BATCH:
        return _err(400, "invalid_input")
    if any(not isinstance(t, str) or not t or len(t) > MAX_TEXT_CHARS for t in texts):
        return _err(400, "invalid_input")



    tokens = max(1, int(sum(sum(1 if ord(c) > 127 else 0.25 for c in t)
                            for t in texts)))
    if not consume_quota(payload["lk"], tokens):
        return _err(429, "quota_exceeded", limit=DAILY_TOKEN_LIMIT)

    vectors = await run_in_threadpool(embed_texts, texts)

    log.info("usage ts=%s lk=%s tokens=%d batch=%d",
             datetime.now(timezone.utc).isoformat(), payload["lk"], tokens, len(texts))
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": vec}
            for i, vec in enumerate(vectors)
        ],
        "model": MODEL_NAME,
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }
