"""Hosted OpenAI-compatible embedding relay для self-hosted инстансов maxgraph.

VPS rezon-ai.ru, nginx проксирует /observer/embeddings сюда (имя пути
историческое — relay теперь embeddings-only). Клиент — embed.py maxgraph,
ждущий OpenAI-compatible POST /embeddings.
"""

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

MODEL_NAME = "intfloat/multilingual-e5-small"  # 384 dims
# Supply-chain пин (SECURITY v0.19.0): ModelSource fastembed 0.9 не принимает
# revision — сверяем sha256 скачанного ONNX с эталоном этой ревизии после загрузки.
MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
MODEL_ONNX_SHA256 = "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665"
DAILY_TOKEN_LIMIT = 200_000  # токенов (chars//4) на lk в UTC-день
MAX_BODY_BYTES = 128 * 1024
MAX_BATCH = 64
MAX_TEXT_CHARS = 8000

log = logging.getLogger("embed-relay")

SECRET = os.environ.get("RELAY_HMAC_SECRET", "")
if not SECRET:
    raise SystemExit("RELAY_HMAC_SECRET is not set")  # fail-fast: без секрета любой токен невалиден

DATA_DIR = Path(os.environ.get("EMBED_DATA_DIR", "data"))
USAGE_FILE = DATA_DIR / "usage.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)  # схема наружу не нужна


# --- auth: base64url(payload).hex(HMAC-SHA256(secret, "relay:" + payload_b64)) ---
# Канон — licensing.relay_token на магазине: подпись HEX-строкой по ПРИНЯТОМУ
# сегменту p64 (пересериализация JSON запрещена), payload_b64 без padding.

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
    except (binascii.Error, ValueError, UnicodeDecodeError):  # ValueError покрывает JSONDecodeError
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("lk"), str):
        return None
    if not isinstance(payload.get("exp"), (int, float)) or payload["exp"] <= time.time():
        return None
    return payload


# --- квота: data/usage.json, {"YYYY-MM-DD": {"<lk>": tokens}}, атомарная запись ---

_usage_lock = threading.Lock()
_usage: dict = {}


def _load_usage(path: Path, store: dict) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # первый запуск или битый файл — начинаем с нуля
    if not isinstance(data, dict):
        return
    for day, rec in data.items():  # валидация вложенности: битый день не роняет сервис
        if isinstance(day, str) and isinstance(rec, dict):
            clean = {str(k): int(v) for k, v in rec.items()
                     if isinstance(v, (int, float)) and not isinstance(v, bool)}
            if clean:
                store[day] = clean


def _consume(store: dict, path: Path, lk: str, tokens: int,
             limit: int | None, charge: bool = True) -> bool:
    """Атомарно проверяет дневную квоту lk и (charge=True) списывает tokens.
    False = квота исчерпана (ничего не списано). limit=None — списать без
    проверки."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _usage_lock:
        for day in [d for d in store if d != today]:
            del store[day]  # храним только текущий UTC-день, файл не разрастается
        day_usage = store.setdefault(today, {})
        used = day_usage.get(lk, 0)
        if limit is not None and used + tokens > limit:
            return False
        if charge:
            day_usage[lk] = used + tokens
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(store), encoding="utf-8")
            os.replace(tmp, path)  # атомарно на одной ФС
        return True


def consume_quota(lk: str, tokens: int) -> bool:
    """Атомарно проверяет и списывает дневную квоту lk. False = квота исчерпана."""
    return _consume(_usage, USAGE_FILE, lk, tokens, DAILY_TOKEN_LIMIT)


# --- v0.28.0: алерт на всплеск разных lk за UTC-день -------------------------
# MED-1 (security-ревизия): триальные токены фармятся self-minted fp — здесь
# ранний СИГНАЛ, не блокировка (стена — per-IP кап выдачи на магазине).
# Считаем lk, прошедшие auth (даже over-quota: фарм виден и по 429).
_distinct_lock = threading.Lock()
_distinct = {"day": "", "lks": set()}
DISTINCT_LK_ALERT_AT = 50  # разных lk за UTC-день → один warning


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
                    "триальных токенов (MED-1 watch)", n)


_load_usage(USAGE_FILE, _usage)


# --- модель: ленивая загрузка, один экземпляр на процесс ---

_model = None
_model_lock = threading.Lock()
# fastembed не гарантирует thread-safety одного экземпляра (onnxruntime-сессия + токенизатор),
# поэтому инференс сериализован; на CPU throughput всё равно упирается в ядра.
_embed_lock = threading.Lock()


def _verify_model_file() -> None:
    """sha256 скачанного ONNX == эталону MODEL_REVISION; расхождение = малварная/
    побитая модель → SystemExit (не стартуем). Внутри get_model — после скачивания."""
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

                # e5-small нет во встроенном реестре fastembed 0.9 (только e5-large) —
                # регистрируем custom-модель на официальных ONNX-весах из HF-репо intfloat.
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


# --- HTTP ---

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
    _note_lk(payload["lk"])  # v0.28.0: алерт-счётчик разных lk/день (MED-1)

    # лимиты до инференса: сначала дешёвые проверки размера, потом парсинг
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0  # мусорный заголовок — не 500, добьёт post-read кап
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

    # оценка квоты: ASCII ≈ chars//4 (как клиент maxgraph), не-ASCII (CJK/emoji)
    # ≈ chars — иначе реальный расход недосчитывается ~4× (SECURITY v0.19.0)
    tokens = max(1, int(sum(sum(1 if ord(c) > 127 else 0.25 for c in t)
                            for t in texts)))
    if not consume_quota(payload["lk"], tokens):
        return _err(429, "quota_exceeded", limit=DAILY_TOKEN_LIMIT)

    vectors = await run_in_threadpool(embed_texts, texts)
    # privacy by design: логируем только usage, контент текстов — никогда
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
