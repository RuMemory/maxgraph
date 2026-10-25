#!/usr/bin/env python3
"""
Maxgraph — память полного сохранения контекста.
MCP (streamable-http) + REST API + неоновый веб-дашборд.

Хранилище (NDJSON, совместимо с @modelcontextprotocol/server-memory):
  memory.json   — состояние графа:
    {"type":"entity","name":...,"title":...,"entityType":...,"tags":[...],
     "observations":[...],"createdAt":...,"updatedAt":...,"project":<kebab-slug?>}
    {"type":"relation","from":...,"to":...,"relationType":...,"createdAt":...}
  events.jsonl  — журнал событий (timeline/аудит):
    {"ts":...,"action":...,"entity":...,"detail":...,"project":<kebab-slug?>}

Проекты: необязательный параметр `project`
(kebab-slug) во всех MCP-инструментах и REST memory-операциях. Пустой project =
плоский граф — поведение как без проектов, default-проект не навязывается. Изоляция чтения:
project=X видит только X (+ сущность project:<slug>); all_projects=true — явный
кросс-поиск. Сущность project:<slug> (entityType "project") автосоздаётся при
первой записи с этим slug; новые сущности связываются с ней «part of».

Env:
  MEMORY_TOKEN  — bearer-токен (Authorization: Bearer, точное совпадение) для всех
                  эндпоинтов вне публичных — полный список в TokenAuth.PUBLIC_PATHS;
                  аутентификации по query нет (удалена: токен не должен оседать в access-логах).
                  Не задан → автогенерация mg_local_* в <data>/local_token.txt
  Телеметрия (всегда включена, выключателя нет):
                  анонимный install-ping — один POST {install_id, version, fp?}
                  на магазин при старте. fp — sha256-хеш machine fingerprint;
                  магазин отвечает якорем начала триала → снос data/ не сбрасывает 7 дней.
  INSTALL_FLAVOR — метка способа установки (docker|docker-desktop|python; rancher =
                  легаси-установки) в install-ping; ставится установщиком
                  (install.sh/install.ps1), пусто → autodetect по /.dockerenv
  MEMORY_DIR    — каталог данных (default /data; native exe: ./data рядом с бинарником)
  MG_HOST       — адрес bind (default: exe → 127.0.0.1, иначе 0.0.0.0)
  PORT          — порт (default 8787)
  READONLY      — READONLY=1: зеркало (resync.py перезаписывает memory.json из прода) —
                  мутации памяти отклоняются (HTTP 403 {"error":"readonly_mirror"}),
                  чтение/поиск/экспорт/дашборд/healthz работают; биллинг не затрагивается
  ACTR_FLUSH_SEC      — период flush счётчиков обращений в data/access.json
                        (default 300; + финальный flush на shutdown через atexit)
  ACTR_MAX_BOOST      — потолок ACT-R буста ранга (default 0.25 = +25%;
                        0 — буст выключен, поведение byte-identical)
  ACTR_HALF_LIFE_DAYS — полураспад recency-фактора в днях (default 14)
"""

import atexit
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.responses import FileResponse
from starlette.responses import Response  # --- digiseller per-order ---

import licensing   # stdlib-only: триал/лицензии (тестируется отдельно)
import hostfp      # stdlib-only: композитный host-fingerprint (hostfp.json от установщика)
import plans       # stdlib-only: тарифы/лейблы планов (общие с магазином)
import searchrank  # --- stdlib-only: ранжированный FTS-lite поиск
import embed       # --- (волна 2) --- stdlib-only: опциональные эмбеддинги, гибридный поиск
import actr        # --- (волна 2) --- stdlib-only: ACT-R reinforcement (частые факты всплывают)

# Компоненты магазина: в self-hosted сборке их нет; роуты, которые ими пользуются,
# и так отвечают 404 через _store_gate() (STORE_MODE=0) — импорт терпимый.
try:
    import billing
except ImportError:
    billing = None
try:
    import digiseller
except ImportError:
    digiseller = None
try:
    import webhooks
except ImportError:
    webhooks = None
try:
    import lava
except ImportError:
    lava = None
try:
    import ghhook
except ImportError:
    ghhook = None

_FROZEN = getattr(sys, "frozen", False)  # True в PyInstaller-бинарнике (native-клиент без Docker)
# Ресурсы (VERSION, html, static/) в exe лежат в распакованном sys._MEIPASS; в исходниках — рядом с app.py
_RESOURCE_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _default_data_dir(env=None, frozen=None) -> str:
    """exe: данные рядом с бинарником (переносимая установка); сервер/Docker: /data."""
    env = os.environ if env is None else env
    frozen = _FROZEN if frozen is None else frozen
    if env.get("MEMORY_DIR"):
        return env["MEMORY_DIR"]
    return str(Path(sys.executable).resolve().parent / "data") if frozen else "/data"


def _bind_host(env=None, frozen=None) -> str:
    """exe — только loopback (нет изоляции Docker-сети); сервер/Docker — 0.0.0.0."""
    env = os.environ if env is None else env
    frozen = _FROZEN if frozen is None else frozen
    return env.get("MG_HOST") or ("127.0.0.1" if frozen else "0.0.0.0")


def _find_version() -> str:
    # Docker: /app/VERSION (копируется Dockerfile'ом); exe: _MEIPASS/VERSION; локальный запуск из app/: ../VERSION
    for p in (_RESOURCE_BASE / "VERSION", _RESOURCE_BASE.parent / "VERSION"):
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    return "0.0.0-dev"


_TOKEN_SOURCE = ""  # env|file|generated|ephemeral — main() логирует токен один раз при генерации


def _resolve_token(env=None, data_dir=None) -> str:
    """MEMORY_TOKEN: env побеждает; иначе автогенерация mg_local_* с персистом
    в <data>/local_token.txt (онбординг без шага «придумай токен»).
    Диск недоступен → эфемерный токен на процесс, без исключений.
    Запись атомарная (tmp+os.replace): crash mid-write не ротует токен."""
    global _TOKEN_SOURCE
    env = env if env is not None else os.environ
    tok = (env.get("MEMORY_TOKEN") or "").strip()
    if tok:
        _TOKEN_SOURCE = "env"
        return tok
    f = (data_dir or DATA_DIR) / "local_token.txt"
    try:
        if f.exists():
            saved = f.read_text(encoding="utf-8").strip()
            if saved:
                _TOKEN_SOURCE = "file"
                return saved
        tok = "mg_local_" + secrets.token_hex(24)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(tok, encoding="utf-8")
        os.replace(tmp, f)
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass  # Windows: chmod частичен — не критично
        _TOKEN_SOURCE = "generated"
        return tok
    except OSError:
        _TOKEN_SOURCE = "ephemeral"
        return "mg_local_" + secrets.token_hex(24)


def _require_store_token():
    """STORE_MODE=1 без явного MEMORY_TOKEN → fail-fast: молчаливая ротация
    мастер-токена роняет клиентов/админку (401) и светит новый токен."""
    if STORE_MODE and not _ENV_TOKEN:
        raise SystemExit("STORE_MODE=1 требует явный MEMORY_TOKEN в env (автогенерация запрещена)")


VERSION = _find_version()
DATA_DIR = Path(_default_data_dir())
HOST = _bind_host()  # exe → 127.0.0.1; иначе 0.0.0.0 (изоляция = docker-compose мэппинг); MG_HOST перекрывает
MEMORY_FILE = DATA_DIR / "memory.json"
PROJECTS_DIR = DATA_DIR / "projects"  # шарды графа по проектам
EVENTS_FILE = DATA_DIR / "events.jsonl"
INSTALLS_FILE = DATA_DIR / "installs.json"  # --- install-ping учёт ---
HEARTBEATS_FILE = DATA_DIR / "heartbeats.jsonl"  # --- (native-client): heartbeat живости ---
SUPPORT_REPORTS_FILE = DATA_DIR / "support_reports.jsonl"  # --- отчёты о багах из дашборда ---
UPDATE_LOG_FILE = DATA_DIR / "update.log"    # журнал helper'а автообновлений (selfupdate)
SERVER_LOG_FILE = DATA_DIR / "maxgraph.log"  # журнал сервера (windowed-exe, ротация 2×1МБ)
_ENV_TOKEN = bool((os.environ.get("MEMORY_TOKEN") or "").strip())
TOKEN = _resolve_token()
WEBHOOK_SECRET = os.environ.get("PLATEGA_WEBHOOK_SECRET", "")
PLATEGA_MERCHANT_ID = os.environ.get("PLATEGA_MERCHANT_ID", "")
PLATEGA_API_KEY = os.environ.get("PLATEGA_API_KEY", "")
PLATEGA_API = os.environ.get("PLATEGA_API_BASE") or (billing.DEFAULT_API_BASE if billing else "")
PLATEGA_CURRENCY = os.environ.get("PLATEGA_CURRENCY") or "RUB"
# --- lava.top (EN-витрина, USD): ключи только на магазине; LAVA_OFFER_ID —
# id оффера продукта (дефолт в lava.py, проверен GET /api/v2/products 2026-10-19)
LAVA_API_KEY = os.environ.get("LAVA_API_KEY", "")
LAVA_WEBHOOK_KEY = os.environ.get("LAVA_WEBHOOK_KEY", "")
LAVA_OFFER_ID = os.environ.get("LAVA_OFFER_ID") or (lava.DEFAULT_OFFER_ID if lava else "")
LAVA_API = os.environ.get("LAVA_API_BASE") or (lava.DEFAULT_API_BASE if lava else "")
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "https://rezon-ai.ru").rstrip("/")
# Каталог релизных tarball'ов для автообновлений: наполняет деплой-скрипт
# магазина (git archive тега + sha256). Наружу только чтение текущей VERSION —
# имя файла не приходит из запроса → path traversal невозможен.
RELEASES_DIR = os.environ.get("RELEASES_DIR") or str(DATA_DIR / "releases")
# NB: docker-compose подставляет ${VAR:-} → env ПУСТАЯ, а не отсутствует — поэтому `or`, а не get(default)
# Планы только Pro, лесенка длительности. tier лицензии всегда "pro" —
# длительность живёт в days (→ exp ключа). Скидки от базы 149₽/мес:
# pro3 −11%, pro6 −16%, pro12 −27%. Цены/лейблы — единый источник plans.PRICES/LABELS.
_PLAN_DAYS = {"pro1": 31, "pro3": 93, "pro6": 186, "pro12": 372}
PLANS = {pid: {**plans.PRICES[pid], "days": _PLAN_DAYS[pid], "label": plans.LABELS[pid]}
         for pid in _PLAN_DAYS}
# донаты — чистая поддержка проекта: days=0, без кода/ключа/лицензии,
# thank-you письмо вместо выдачи. В /api/plans и лицензии не попадают.
_DONATE = ("donate99", "donate249", "donate499")
PLANS.update({pid: {**plans.PRICES[pid], "days": 0, "label": plans.LABELS[pid],
                    "donation": True} for pid in _DONATE})
PLAN_ALIASES = {"pro": "pro1", "team": "pro1"}  # легаси-вызовы (старые платежи хранят tier="pro"/"team"; team был месячным → pro1)


def _plan_key(name: str) -> str:
    """Алиасы + валидация: 'pro' → 'pro1'; неизвестное → ''."""
    pid = (name or "").strip()
    if pid in PLAN_ALIASES:
        return PLAN_ALIASES[pid]
    return pid if pid in PLANS else ""
# --- digiseller per-order --- /billing/digiseller/gen — код по заказу Digiseller
DIGISELLER_GEN_SECRET = os.environ.get("DIGISELLER_GEN_SECRET", "")
DIGISELLER_SIGN_SCHEME = os.environ.get("DIGISELLER_SIGN_SCHEME", "url-key")
DIGISELLER_RESPONSE_FORMAT = os.environ.get("DIGISELLER_RESPONSE_FORMAT", "text")
DIGISELLER_PRODUCT_ID = os.environ.get("DIGISELLER_PRODUCT_ID", "XXXXXXXX")
# --- digiseller уведомления: /webhook/digiseller/{sale,message} → лог + Telegram ---
DIGISELLER_HOOK_SECRET = os.environ.get("DIGISELLER_HOOK_SECRET", "")
TG_NOTIFY_CHAT_ID = os.environ.get("TG_NOTIFY_CHAT_ID", "")
# STORE_MODE=1 — инстанс магазина (rezon-ai.ru): /api/license/activate убирается из публичных
STORE_MODE = (os.environ.get("STORE_MODE") or "").strip() == "1"
# READONLY=1 — read-only зеркало прода (resync.py перезаписывает memory.json каждые 5 мин):
# мутации памяти запрещены (иначе записи молча стираются ресинком), чтение работает
READONLY = (os.environ.get("READONLY") or "").strip() == "1"
WEBHOOKS_LOG = DATA_DIR / "webhooks.jsonl"
EMBEDDINGS_FILE = DATA_DIR / "embeddings.json"     # {name: {"v": [...], "u": updatedAt}}
EMBED_STATE_FILE = DATA_DIR / "embed.state.json"   # дневной кап токенов (len/4)
ACCESS_FILE = DATA_DIR / "access.json"  # ACT-R счётчики обращений {name: {"n","t"}}
LICENSE_SECRET = licensing.get_secret(DATA_DIR)  # env LICENSE_HMAC_SECRET или DATA_DIR/.hmac_key
LICENSE_SUB_DAYS = 31  # легаси-fallback (digiseller без суммы); реальный срок = PLANS[plan]["days"]
EXTEND_DISCOUNT = 0.9  # продление −10% — применяется серверно в /api/billing/pay
# --- реферальная система ---
# Секрет деривации реф-кодов (HMAC от install_id). Прод — строго env; дефолт — только
# для тестов/dev (compose подставляет пустую строку → `or`, не get(default)).
REF_HMAC_SECRET = (os.environ.get("REF_HMAC_SECRET") or "").strip() or "dev-only-ref-secret"  # dev-дефолт только для локалки
REF_DISCOUNT = 0.8   # −20% приглашённому на первый платёж тарифа (только Platega-контур)
REF_STACK_CAP = 4    # +10 п.п. за покупку приглашённого; кап 4 → продление до −50% (стек включает базу −10%)
PORT = int(os.environ.get("PORT", "8787"))
STARTED = time.time()
EMBED_ON = False
EVENTS_CAP = 10000  # максимум строк в events.jsonl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
if _FROZEN:  # windowed-exe: консоли нет — stderr никто не увидит.
    try:     # Дублируем лог в data/maxgraph.log (ротация 2×1МБ), иначе падения слепые.
        from logging.handlers import RotatingFileHandler
        _fh = RotatingFileHandler(DATA_DIR / "maxgraph.log", maxBytes=1_000_000,
                                  backupCount=2, encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(_fh)
    except OSError:
        pass
log = logging.getLogger("maxgraph")

# На магазине без явного секрета коды вычислимы офлайн по install_id — не стартуем
# (импортное время: raise до uvicorn.run, процесса не будет вовсе)
if STORE_MODE and REF_HMAC_SECRET == "dev-only-ref-secret":
    # fail-fast: магазин с dev-дефолтом выдаёт офлайн-вычислимые реф-коды — не стартуем
    raise SystemExit("REF_HMAC_SECRET не задан в .env — задай секрет (магазин без него не стартует)")

_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- storage ---

def _load():
    """Load graph from NDJSON -> (entities dict, relations list).

    Шарды: memory.json (плоское) + projects/<slug>.json — читаются в те же
    структуры; read-путь дальше не знает про шарды. Связи с отсутствующим концом
    отбрасываются (удалённый шард проекта не оставляет
    висячих связей)."""
    entities, relations = {}, []
    files = []
    if MEMORY_FILE.exists():
        files.append(MEMORY_FILE)
    if PROJECTS_DIR.is_dir():
        files.extend(sorted(PROJECTS_DIR.glob("*.json")))
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") == "entity" and rec.get("name"):
                ent = {
                    "name": rec["name"],
                    "title": rec.get("title") or rec["name"],
                    "entityType": rec.get("entityType", "general"),
                    "tags": list(rec.get("tags", [])),
                    "observations": list(rec.get("observations", [])),
                    "createdAt": rec.get("createdAt", ""),
                    "updatedAt": rec.get("updatedAt", rec.get("createdAt", "")),
                }
                if rec.get("project"):  # ключ только когда задан — legacy-строки не меняются
                    ent["project"] = rec["project"]
                if rec.get("status"):  # явный статус бага; legacy-строки без него не меняются
                    ent["status"] = rec["status"]
                entities[rec["name"]] = ent
            elif rec.get("type") == "relation":
                relations.append({
                    "from": rec.get("from", ""),
                    "to": rec.get("to", ""),
                    "relationType": rec.get("relationType", ""),
                    "createdAt": rec.get("createdAt", ""),
                })
    dropped = len(relations)
    relations = [r for r in relations if r["from"] in entities and r["to"] in entities]
    if len(relations) != dropped:
        log.info("load: отброшено связей с отсутствующим концом: %d", dropped - len(relations))
    # дедуп по тройке: restore монолита поверх шардов
    # иначе дублирует связи (монолит-union + старый шард читаются вместе)
    seen = set()
    relations = [r for r in relations
                 if (r["from"], r["to"], r["relationType"]) not in seen
                 and not seen.add((r["from"], r["to"], r["relationType"]))]
    return entities, relations


_shard_written = {}  # path -> sha256 последнего записанного содержимого


# Windows-reserved имена файлов slug «con»/«nul»/…
# проходит regex, но open() на Windows-хосте падает — валим на обеих границах.
_RESERVED_SLUGS = frozenset({"con", "prn", "aux", "nul"}
                            | {f"com{i}" for i in range(1, 10)}
                            | {f"lpt{i}" for i in range(1, 10)})


def _shard_of_entity(e: dict) -> str:
    """Шард сущности = её project; мусорный slug из файла → плоский шард
    (сам файл — тоже граница доверия: projects/<slug>.json собирается из записи)."""
    p = e.get("project") or ""
    return p if len(p) <= 64 and _PROJECT_RE.fullmatch(p) and p not in _RESERVED_SLUGS else ""


def _partition_shards(entities: dict, relations: list) -> dict:
    """→ {slug: текст шарда}; "" = плоский memory.json (пишется всегда — иначе
    опустевший граф не очистил бы файл). Связь — в шард проекта, только если ОБА
    конца в нём; иначе плоская (правило «по концам»)."""
    buckets = {"": []}
    for e in entities.values():
        buckets.setdefault(_shard_of_entity(e), []).append({"type": "entity", **e})
    for r in relations:
        pa = _shard_of_entity(entities.get(r["from"], {}))
        pb = _shard_of_entity(entities.get(r["to"], {}))
        buckets.setdefault(pa if pa == pb else "", []).append({"type": "relation", **r})
    return {slug: "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in recs)
            for slug, recs in buckets.items()}


def _save(entities, relations):
    """Шард-запись, двухфазно: сначала ВСЕ .tmp, потом ВСЕ replace — окно
    рассинхрона = rename-only последовательность. Неизменённый шард не трогаем
    (кэш sha256; валиден, пока файлы пишет только этот процесс — READONLY-зеркало
    не пишет). Файл опустевшего проекта удаляется (иначе _load воскресит удалённое)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    shards = _partition_shards(entities, relations)
    if PROJECTS_DIR.is_dir():  # опустевшие проекты: шард должен исчезнуть
        for f in PROJECTS_DIR.glob("*.json"):
            if f.name[:-5] not in shards:
                _shard_written.pop(f, None)
                try:
                    f.unlink()
                except OSError:
                    log.error("save: не смог удалить пустой шард %s — проект воскреснет при чтении; "
                              "удалите файл вручную и перезапустите", f)
    pending = []
    for slug, text in shards.items():
        path = MEMORY_FILE if not slug else PROJECTS_DIR / f"{slug}.json"
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if _shard_written.get(path) == h and path.exists():
            continue
        pending.append((path, h, text))
    if not pending:
        return
    tmps = []
    try:
        for path, h, text in pending:  # фаза 1: все tmp
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            tmps.append((tmp, path, h))
        for tmp, path, h in tmps:  # фаза 2: все replace
            tmp.replace(path)
            _shard_written[path] = h
    finally:
        for tmp, _, _ in tmps:  # сбой любой фазы — tmp не мусорят
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


def _migrate_to_shards():
    """Одноразовая миграция на шарды: монолитный memory.json →
    projects/<slug>.json по полю project. Триггер: memory.json есть, сентинела
    projects/.sharded нет (сентинел — ПОСЛЕ записи: kill -9 посреди replace-фазы
    не пометит миграцию выполненной). Полный слепок до разрезки —
    memory.json.pre-shards-backup (не удалять и не затирать повторно; откат
    на старую версию = скопировать его обратно). READONLY: миграции нет — записи
    запрещены, зеркало хранит объединение плоско (см. resync)."""
    sentinel = PROJECTS_DIR / ".sharded"
    if READONLY or not MEMORY_FILE.exists() or sentinel.exists():
        return
    backup = DATA_DIR / "memory.json.pre-shards-backup"
    if not backup.exists():  # повторный прогон (ручное удаление projects/) не затирает слепок
        backup.write_bytes(MEMORY_FILE.read_bytes())
    ents, rels = _load()
    _save(ents, rels)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)  # при плоской памяти _save
    # не создаёт projects/ — без этого сентинел падал FileNotFoundError
    sentinel.write_text("ok\n", encoding="ascii")  # маркер «миграция была»
    log.info("шардинг: memory.json разрезан (%d сущностей, %d связей), слепок → %s",
             len(ents), len(rels), backup.name)


_REPO_TAG_SHARD_RE = re.compile(r"^repo-(.+)-[0-9a-f]{12}$")


def _heal_repo_tag_projects():
    """Самолечение после удаления механизма repo-тегов (1.5.1): установки,
    где авто-алиас успел увести данные в проект repo-<name>-<hex12>
    (инцидент 2026-10-21 — «пустая память»), получают их обратно: сущности
    переписываются на проект <name>, события пульса — тоже, project:<tag>
    сливается в project:<name>. Идемпотентно: без repo-проектов — no-op.
    READONLY: пропуск (записи запрещены)."""
    if READONLY:
        return
    try:
        ents, rels = _load()
    except Exception as ex:
        log.warning("repo-tag heal: граф не читается: %s", ex)
        return
    moves = {}  # tag → legacy name
    for e in ents.values():
        m = _REPO_TAG_SHARD_RE.match(e.get("project") or "")
        if m:
            moves[e["project"]] = m.group(1)
    if not moves:
        return
    moved = 0
    for e in ents.values():
        tgt = moves.get(e.get("project") or "")
        if tgt:
            e["project"] = tgt
            moved += 1
    for tag, legacy in moves.items():
        tag_pe = ents.pop(_project_entity_name(tag), None)
        if tag_pe is None:
            continue
        canon_pe = ents.get(_project_entity_name(legacy))
        obs = tag_pe.get("observations") or []
        if canon_pe is not None:
            have = {_norm_obs(o) for o in (canon_pe.get("observations") or [])}
            for o in obs:
                if _norm_obs(o) not in have:
                    canon_pe.setdefault("observations", []).append(o)
        else:
            tag_pe["name"] = _project_entity_name(legacy)
            tag_pe["title"] = legacy
            ents[tag_pe["name"]] = tag_pe
        # связи на старое имя сущности проекта — на новое (merge и rename)
        for r in rels:
            if r.get("from") == _project_entity_name(tag):
                r["from"] = _project_entity_name(legacy)
            if r.get("to") == _project_entity_name(tag):
                r["to"] = _project_entity_name(legacy)
    _save(ents, rels)
    # пульс: события repo-тега → legacy (JSONL; битые строки — как есть)
    ev_moved = 0
    try:
        if EVENTS_FILE.exists():
            out_lines = []
            for line in EVENTS_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    out_lines.append(line)
                    continue
                if r.get("project") in moves:
                    r["project"] = moves[r["project"]]
                    ev_moved += 1
                out_lines.append(json.dumps(r, ensure_ascii=False))
            tmp = EVENTS_FILE.with_suffix(".tmp")
            tmp.write_text("".join(l + "\n" for l in out_lines), encoding="utf-8")
            tmp.replace(EVENTS_FILE)
    except Exception as ex:
        log.warning("repo-tag heal: пульс не мигрирован: %s", ex)
    log.info("repo-tag heal: %s (сущностей %d, событий %d)", moves, moved, ev_moved)


def _graph_file_bytes() -> int:
    """Размер хранилища графа для дашборда: memory.json + все шарды."""
    total = MEMORY_FILE.stat().st_size if MEMORY_FILE.exists() else 0
    if PROJECTS_DIR.is_dir():
        total += sum(f.stat().st_size for f in PROJECTS_DIR.glob("*.json"))
    return total


def _test_purge_graph_files():
    """Тест-хелпер: полный сброс файлов графа (memory.json + projects/ + кэш шардов).
    Тесты чистят MEMORY_FILE руками — с шардами этого мало (утечка между тестами)."""
    if MEMORY_FILE.exists():
        MEMORY_FILE.unlink()
    if PROJECTS_DIR.is_dir():
        for f in PROJECTS_DIR.glob("*.json"):
            f.unlink()
    _shard_written.clear()


def _event(action: str, entity: str = "", detail: str = "", project: str = "",
           source: str = ""):
    """Append event to the audit log (timeline). project — slug проекта, если задан.
    source — кто породил событие: "" (default) — пользователь/агент/сервер;
    "hook" — захват-хук хоста."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ev = {"ts": now_iso(), "action": action, "entity": entity, "detail": detail}
        if project:
            ev["project"] = project
        if source:
            ev["source"] = source
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        # мягкая ротация
        if EVENTS_FILE.stat().st_size > 4_000_000:
            lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()[-EVENTS_CAP:]
            EVENTS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as ex:  # журнал не должен ронять запись
        log.warning("event log failed: %s", ex)


# ------------------------------------------------------------ ACT-R ---
# (аналог ACT-R reinforcement у claude-mem): частые и недавние обращения
# поднимают сущность в выдаче — память сама учится, что важно. Счётчики
# in-memory (НЕ в memory.json: каждый search = fsync-шторм, а правка updatedAt
# сломала бы recency-скоринг и кэш searchrank, чей ключ — (name, updatedAt));
# flush в access.json раз в ACTR_FLUSH_SEC (daemon-поток actr_loop) и при
# shutdown (atexit, поток daemon'ный, graceful = atexit).
# Пульс НЕ пишется (фоновый тик засорял бы timeline), события flush — нет.
_actr = actr.Store()
_actr.load(ACCESS_FILE)  # старт: нет/битый access.json → с нуля (fail-open)


def _touch(names):
    """учёт «осмысленных» чтений — search (top-20 выдачи), open_nodes,
    session_start (сущности, попавшие в context_block). НЕ read_graph/пустой
    query: дамп всего графа — не сигнал интереса к конкретным фактам.
    O(1) на имя, leaf-лок внутри Store (без I/O/сети/чужих локов — порядок
    локов не нарушается: _actr-лок не захватывается под _lock/_codes_lock)."""
    _actr.touch(names)


def _actr_boost_fn():
    """actr_fn name→множитель ∈ [1, 1+ACTR_MAX_BOOST] для searchrank.rank
    и сортировок session_start; None, когда буст выключен (ACTR_MAX_BOOST=0) —
    rank тогда идёт веткой чистого FTS — поведение не меняется. Env читается на каждый
    вызов (как _hybrid_search у embed): конфиг подхватывается без рестарта."""
    cfg = actr.config_from_env()
    if cfg["max_boost"] <= 0.0:
        return None
    return actr.boost_fn(_actr, cfg)


# ------------------------------------------------------------- cold (1.9.0) ---
# Мягкая авто-архивация: cold — ВЫЧИСЛЯЕМЫЙ на чтении статус, из read-путей
# в шарды ничего не пишется. last_access = ACT-R t (последнее обращение),
# фолбэк — updatedAt|createdAt (сущность «свежая» при создании). Revive
# отдельного кода не требует: open_nodes / попадание в выдачу — это уже _touch.
COLD_AFTER_DAYS_DEFAULT = 90


def _cold_after_days() -> int:
    """COLD_AFTER_DAYS из env: 0 — выкл; пусто/мусор/<0 → default 90.
    Читается на каждый вызов (как _actr_boost_fn): конфиг без рестарта."""
    raw = str(os.environ.get("COLD_AFTER_DAYS") or "").strip()
    if not raw:
        return COLD_AFTER_DAYS_DEFAULT
    try:
        v = int(raw)
    except ValueError:
        return COLD_AFTER_DAYS_DEFAULT
    return v if v >= 0 else COLD_AFTER_DAYS_DEFAULT


def _field_ts(entity: dict, field: str) -> float:
    """entity[field] (ISO) → epoch seconds; битое/пустое → 0.0."""
    try:
        dt = datetime.fromisoformat(str(entity.get(field) or ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _is_cold(entity: dict, now: float) -> bool:
    """cold = сущность не востребована COLD_AFTER_DAYS+ дней (строго больше:
    ровно на границе — ещё тёплая). Выкл (0) → всегда False; возраст
    неизвестен (ни обращений, ни валидных дат) → False: не знаем — не прячем.
    Read-only, O(1)."""
    days = _cold_after_days()
    if days <= 0:
        return False
    last = _actr.last_ts(entity["name"])
    if last <= 0:
        # max: write-path всегда даёт updatedAt ≥ createdAt, поэтому на реальных
        # данных это ровно updatedAt из спеки; createdAt страхует от ручной
        # правки файла (updatedAt старше createdAt) — «свежая при создании»
        last = max(_field_ts(entity, "updatedAt"), _field_ts(entity, "createdAt"))
    return last > 0 and (now - last) > days * 86400


# ------------------------------------------------------------- projects -----

# опциональный слой проектов. slug валидируется на границе
# (MCP-инструменты/REST-хендлеры), op_* принимают уже нормализованный slug ("" = без проекта).
_PROJECT_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


def _check_project(value) -> tuple[str, str]:
    """Нормализация/валидация slug'а проекта на границе → (slug, "") | ("", ошибка).

    Пустой ввод → ("", ""): проект не задан, плоский граф. Регистр приводим
    к нижнему: 'Kimi' и 'kimi' — один проект.
    """
    slug = str(value or "").strip().lower()
    if not slug:
        return "", ""
    if len(slug) > 64 or not _PROJECT_RE.fullmatch(slug) or slug in _RESERVED_SLUGS:
        return "", f"invalid project slug {value!r}: kebab-case [a-z0-9-], ≤64 chars"
    return slug, ""


def _truthy(value) -> bool:
    """?all_projects=1|true|yes — парсинг bool из query."""
    return str(value or "").strip().lower() in ("1", "true", "yes")


def _query_project(request) -> tuple[str, str]:
    """project из query GET: (slug, "") | ("", ошибка). Граница валидации."""
    return _check_project(request.query_params.get("project"))


def _project_entity_name(slug: str) -> str:
    return f"project:{slug}"


# Спецпроект 1.7.0: decision-сущности global домешиваются в session_start
# любого проекта («## Глобальная память»); перенос — только promote/demote.
GLOBAL_PROJECT = "global"


def _in_project(entity: dict, slug: str) -> bool:
    """Сущность адресуема в контексте проекта: её project == slug, либо это сама
    сущность проекта project:<slug> (она структурная, сама проекту не принадлежит)."""
    return (entity.get("project") or "") == slug or entity["name"] == _project_entity_name(slug)


def _filter_graph(ents: dict, rels: list, slug: str, all_projects: bool):
    """Изоляция: project=X → сущности проекта X (+ project:X), связи — оба конца
    видимы. Без project или all_projects=true — полный граф (обратная совместимость)."""
    if not slug or all_projects:
        return list(ents.values()), rels
    es = [e for e in ents.values() if _in_project(e, slug)]
    names = {e["name"] for e in es}
    return es, [r for r in rels if r["from"] in names and r["to"] in names]


# ------------------------------------------------------------- taxonomy -----

# канонические типы сущностей. Свободные типы НЕ ломаются:
# неизвестный тип остаётся как есть — канонизируются только словарные синонимы
# (RU/EN, регистронезависимо). Подсказки каноники: описания MCP-инструментов,
# docs/PROTOCOL.md §1.2, чипы-фильтры дашборда.
CANONICAL_TYPES = ("project", "session", "bug", "decision", "request", "event", "task")

_TYPE_SYNONYMS = {
    # канонические — тождественно (заодно схлопывает регистр: "Bug" → "bug")
    "project": "project", "проект": "project",
    "session": "session", "сессия": "session",
    "bug": "bug", "bugfix": "bug", "fix": "bug", "hotfix": "bug", "defect": "bug",
    "баг": "bug", "багфикс": "bug", "фикс": "bug", "ошибка": "bug",
    "decision": "decision", "решение": "decision",
    "request": "request", "feature-request": "request",
    "просьба": "request", "запрос": "request", "хотелка": "request",
    "event": "event", "событие": "event",
    # (work_state): открытые дела проекта
    "task": "task", "todo": "task", "задача": "task", "туду": "task", "дело": "task",
}

# Явный статус (bug, task): только перечисленные значения.
# default при создании — open.
BUG_STATUSES = ("open", "fixed")
TASK_STATUSES = ("open", "done")
# Типы, у которых status — первоклассное поле; для остальных — игнор с warning.
_STATUS_BY_TYPE = {"bug": BUG_STATUSES, "task": TASK_STATUSES}


def _canonical_type(value) -> str:
    """Нормализация entityType на границе записи: словарный синоним (RU/EN,
    любой регистр) → канонический тип; свободный неизвестный тип — как есть."""
    t = str(value or "").strip()
    if not t:
        return "general"
    return _TYPE_SYNONYMS.get(t.lower(), t)


def _check_status(value, etype: str) -> tuple[str, str]:
    """Нормализация/валидация status для типа etype: (status, "") | ("", ошибка).

    Пустой ввод → ("", ""): статус не задан (bug/task подразумевают open).
    Для типов со статусом (bug: open/fixed, task: open/done) невалидное значение —
    ошибка. Для типов БЕЗ статуса нормализованное значение возвращается как есть —
    решение о warning («применим только к bug/task») принимает caller, он видит
    контекст (имя сущности)."""
    st = str(value or "").strip().lower()
    if not st:
        return "", ""
    valid = _STATUS_BY_TYPE.get(etype)
    if valid and st not in valid:
        return "", f"invalid {etype} status {value!r}: {'|'.join(valid)}"
    return st, ""


# --- авто-тайпинг (1.8.0): подсказка типа при create_entities -------------
# Детерминированные RU/EN-регексы по title+наблюдениям, приоритет
# bug > decision > task > request > session (первое совпадение побеждает).
# ТОЛЬКО совет (hints.type_suggestion в ответе): данные не меняются, агент
# может игнорировать; нет совпадения → подсказки нет (fail-open).
_TYPE_SUGGEST_RULES = tuple(
    (t, re.compile(p, re.IGNORECASE)) for t, p in (
        ("bug", r"баг|bug|сломан|падает|crash|error|fix|почини"),
        ("decision", r"решил|решение|decision|утверждено|approved|выбрали"),
        ("task", r"надо|нужно сделать|todo|task|задача|доделать"),
        ("request", r"просьба|request|хотелка"),
        ("session", r"сессия|session"),
    ))


def _suggest_type(title, observations):
    """→ {"suggested", "matched_rule"} | None. matched_rule — сработавший паттерн."""
    text = " ".join([str(title or "")] + [_obs_text(o) for o in (observations or [])])
    for suggested, rx in _TYPE_SUGGEST_RULES:
        if rx.search(text):
            return {"suggested": suggested, "matched_rule": rx.pattern}
    return None


# ------------------------------------------------------------- graph ops ----

def _obs_text(o) -> str:
    """Текст наблюдения: строка → как есть; объект {text, source} (1.6.0) → text."""
    if isinstance(o, dict):
        # файл шарда — граница доверия: руками подпорченный dict без text
        # не должен ронять все read-пути
        t = o.get("text")
        return t if isinstance(t, str) else ""
    return str(o)


def _obs_source(o):
    """Структурный источник наблюдения {kind, ref} (1.6.0) или None."""
    src = o.get("source") if isinstance(o, dict) else None
    return src if isinstance(src, dict) else None


def _norm_obs(s) -> str:
    """Ключ дедупликации фактов (эпик «устойчивость к компактизации», слой 2):
    регистр, повторные пробелы и краевая пунктуация не делают факт новым.
    Хранится оригинал — ключ нужен только для сравнения (по тексту: _obs_text,
    источник объекта в дедупе не участвует). Строка из одной
    пунктуации нормализуется в пустую → fallback на оригинал."""
    r = " ".join(_obs_text(s).casefold().split()).strip(" .,;:!?…")
    return r or _obs_text(s).strip()


# --- инвалидация устаревших фактов --------
# Устаревший факт НЕ удаляется, а помечается префиксом-маркером [~YYYY-MM-DD]
# (археология: текст остаётся в данных, delete_observations работает всегда).
# Читающие пути: session_start и search показывают/скорят только валидные строки;
# read_graph/open_nodes — как есть (полная правда, включая помеченные).

_INVALIDATED_RE = re.compile(r"^\[~[0-9]{4}-[0-9]{2}-[0-9]{2}\]\s*")


def _is_invalidated(obs) -> bool:
    s = _obs_text(obs)
    return s.startswith("[~") and bool(_INVALIDATED_RE.match(s))  # fast-path


def _strip_marker_on_write(obs):
    """Входящий факт с маркером [~дата] → маркер срезаем
    (иначе запись рождается невидимой для search/session_start — копипаст из
    read_graph при ре-прайме). Объект {text, source} срезается внутри text,
    source сохраняется. → (значение, был_маркер)."""
    s = _obs_text(obs)
    if _is_invalidated(s):
        s = _INVALIDATED_RE.sub("", s, count=1)
        return ({**obs, "text": s} if isinstance(obs, dict) else s), True
    return obs, False


def _valid_obs(e: dict) -> list:
    """Наблюдения сущности без инвалидированных."""
    return [o for o in e.get("observations", []) if not _is_invalidated(o)]


def _mark_observation(obs, today: str):
    """Поставить маркер инвалидации (идемпотентно: уже помеченная — как есть).
    Объект с source (1.6.0): маркер ставится в поле text, source сохраняется."""
    if _is_invalidated(obs):
        return obs
    if isinstance(obs, dict):
        return {**obs, "text": f"[~{today}] {_obs_text(obs)}"}
    return f"[~{today}] {_obs_text(obs)}"


# --- forget по id ---------------------------------------------------
# Стабильный id наблюдения — sha256 текста КАК ХРАНИТСЯ (с [~дата]-маркером,
# если он есть) — вычисляемый, НЕ хранимый: старые записи работают сразу, а
# «что видишь в выдаче with_ids, то и удаляешь» (delete_observations_by_id).
# Объект {text, source}: id от text — тот же факт строкой имеет тот же id.

def _obs_id(obs) -> str:
    return hashlib.sha256(_obs_text(obs).encode("utf-8")).hexdigest()[:12]


def _obs_with_ids(obs_list) -> list:
    """Наблюдения как объекты {id, text, source?} — формат выдачи with_ids=True."""
    out = []
    for o in obs_list:
        d = {"id": _obs_id(o), "text": _obs_text(o)}
        src = _obs_source(o)
        if src:
            d["source"] = src
        out.append(d)
    return out



def _create_entities_graph(ents, rels, items, project: str = "", source: str = ""):
    """Ядро op_create_entities на УЖЕ загруженном графе (без _load/_save и без
    захвата _lock) — для батчей, где caller держит лок и граф на весь запрос
    (api_ingest SessionEnd: один батч — один _load и один _save). → (out, changed); запись — обязанность caller'а, один _save в конце.

    Вся семантика (merge/adopted/project-автосоздание/part-of/статусы багов/
    события пульса) — здесь, единый источник истины; op_create_entities — тонкая
    обёртка с загрузкой и записью."""
    created = []
    warnings = []
    merged = obs_added = obs_dedup = adopted = 0
    ts = now_iso()
    rel_keys = {(r["from"], r["to"], r["relationType"]) for r in rels}
    rels_added = False

    def link_part_of(name: str):
        nonlocal rels_added
        key = (name, _project_entity_name(project), "part of")
        if key in rel_keys:
            return
        rels.append({"from": name, "to": key[1], "relationType": "part of", "createdAt": ts})
        rel_keys.add(key)
        rels_added = True
        _event("create_relation", f"{name}→{key[1]}", "part of", project=project,
               source=source)

    project_created = False
    if project:
        pname = _project_entity_name(project)
        if pname not in ents:
            ents[pname] = {"name": pname, "title": project, "entityType": "project",
                           "tags": [], "observations": [], "createdAt": ts, "updatedAt": ts}
            project_created = True
            _event("create_entity", pname, project, project=project, source=source)
    for e in items:
        if not str(e.get("name") or "").strip():
            # security-ревизия (LOW, порт 644e2fb): пустое имя — не сущность;
            # заодно закрывает обход гварда псевдо-проектов через name == ""
            warnings.append("пустое name — сущность пропущена")
            continue
        if e["name"] in ents:
            ex = ents[e["name"]]
            ex_project = ex.get("project") or ""
            if project and ex_project and ex_project != project:
                warnings.append(
                    f"'{e['name']}' уже существует в проекте '{ex_project}' — merge "
                    f"в проект '{project}' отклонён (имена сущностей глобально уникальны)")
                continue
            # security-ревизия (MEDIUM, порт 644e2fb, ASCII-форма под 1.2.9):
            # структурный маркер project:<slug> принимает долив фактов только
            # из СВОЕГО проекта — иначе любой проект мог бы писать в чужой
            # «# Проект» блок session_start
            if e["name"].startswith("project:") \
                    and e["name"] != _project_entity_name(project):
                warnings.append(
                    f"'{e['name']}': структурная сущность чужого проекта — "
                    f"merge из проекта '{project}' отклонён")
                continue
            have = {_norm_obs(o) for o in ex["observations"]}
            added = []
            for c in e.get("observations", []):
                c, was_marked = _strip_marker_on_write(c)
                if was_marked:
                    warnings.append(f"'{e['name']}': вход с маркером [~дата] — "
                                    f"маркер срезан (факт не должен рождаться "
                                    f"инвалидированным)")
                k = _norm_obs(c)
                if k in have:
                    obs_dedup += 1
                    continue
                have.add(k)
                added.append(c)
            # слой 2: занятое имя больше не сливается молча — warning всегда
            warnings.append(
                f"'{e['name']}' уже существует — факты долиты merge'ом "
                f"(новых: {len(added)}, дублей: "
                f"{len(e.get('observations', [])) - len(added)}), "
                f"новая сущность не создана")
            if project and not ex_project and not e["name"].startswith("project:"):
                ex["project"] = project  # легаси-сущность принимает проект первого коснувшегося
                ex["updatedAt"] = ts
                adopted += 1
                link_part_of(e["name"])
            if added:
                ex["observations"].extend(added)
                ex["updatedAt"] = ts
                merged += 1
                obs_added += len(added)
                _event("merge_entity", e["name"], f"+{len(added)} obs",
                       project=project or ex_project, source=source)
            continue
        obs_norm, obs_list = set(), []
        for c in e.get("observations", []):
            c, was_marked = _strip_marker_on_write(c)
            if was_marked:
                warnings.append(f"'{e['name']}': вход с маркером [~дата] — "
                                f"маркер срезан (факт не должен рождаться "
                                f"инвалидированным)")
            k = _norm_obs(c)
            if k in obs_norm:
                continue
            obs_norm.add(k)
            obs_list.append(c)
        etype = _canonical_type(e.get("entityType"))
        # security-ревизия r2: префикс project: зарезервирован для структурного
        # маркера СВОЕГО проекта (симметрично merge-гварду выше) — иначе
        # project:<чужой slug> с любым типом, отличным от project, создавался
        # беспрепятственно и травил чужой session_start (маркер читается
        # по имени без проверки типа), а системное автосоздание маркера
        # потом пропускалось (имя занято). Системные создатели маркера
        # (автосоздание выше, api_ingest) идут мимо этого цикла.
        if e["name"].startswith("project:") \
                and e["name"] != _project_entity_name(project):
            warnings.append(
                f"'{e['name']}': имя с префиксом project: зарезервировано для "
                f"структурного маркера своего проекта — сущность не создана")
            continue
        # security-ревизия (MEDIUM, порт 644e2fb, ASCII-форма под 1.2.9):
        # type=project зарезервирован для проектов-рабочих-папок — только
        # структурный маркер СВОЕГО проекта (project:<slug>; создаёт система)
        # и самоссылочная сущность папки (name == свой slug, легаси). Иначе —
        # псевдо-проект или липовый маркер ЧУЖОГО проекта (pre-birth poisoning
        # чужого session_start): сущность не создаётся.
        if etype == "project" and e["name"] != _project_entity_name(project) \
                and e["name"] != project:
            warnings.append(
                f"'{e['name']}': entityType=project зарезервирован для "
                f"проектов-рабочих-папок — сущность не создана")
            continue
        d = {
            "name": e["name"],
            "title": e.get("title") or e["name"],
            "entityType": etype,
            "tags": list(e.get("tags", [])),
            "observations": obs_list,
            "createdAt": ts,
            "updatedAt": ts,
        }
        status, serr = _check_status(e.get("status"), etype)
        if serr:
            warnings.append(f"'{e['name']}': {serr} — проигнорирован")
            status = ""
        if etype in _STATUS_BY_TYPE:
            # явный статус (bug: open/fixed; task: open/done), default open
            d["status"] = status or "open"
        elif status:
            warnings.append(f"'{e['name']}': status применим только к bug/task "
                            f"(тип '{etype}') — проигнорирован")
        # project:* — структурные сущности проектов: сами проекту не принадлежат,
        # part-of на себя не получают (как и в deploy/migrate-projects.py)
        if project and not e["name"].startswith("project:"):
            d["project"] = project
        ents[e["name"]] = d
        created.append(d)
        _event("create_entity", e["name"], d["title"], project=project, source=source)
        if "project" in d:
            link_part_of(e["name"])
    out = {"created": created, "skipped": len(items) - len(created),
           "merged": merged, "observations_added": obs_added,
           "observations_dedup": obs_dedup}
    if adopted:
        out["adopted"] = adopted
    if warnings:
        out["warnings"] = warnings
    changed = bool(created or merged or adopted or project_created or rels_added)
    return out, changed


def op_create_entities(items, project: str = "", source: str = ""):
    """Создание сущностей; существующее имя → MERGE observations (не молчаливый skip).

    Дедуп observations — по нормализованному ключу _norm_obs (регистр/пробелы/
    краевая пунктуация; и против имеющихся, и внутри входящего списка). Merge
    занятого имени → warnings[] (не молчит, слой 2); при включённом embed —
    near-dup warnings по косинусу ≥ _NEARDUP_COS к векторам сущностей проекта.
    Ответ: created — новые сущности; skipped — сколько входящих имён
    НЕ создано (уже существовали; обратная совместимость со старыми клиентами);
    merged — в скольких существующих сущностях реально добавились observations;
    observations_added/observations_dedup — счётчики принятых/отброшенных фактов.

    project: новые сущности получают slug; сущность project:<slug> автосоздаётся
    один раз, каждая новая сущность связывается с ней «part of». Merge разрешён, если
    проект сущности совпадает или не задан (легаси-сущность тогда ПРИНИМАЕТ проект,
    счётчик adopted); конфликт проектов (X ≠ Y) → сущность не трогаем, она считается
    в skipped, в ответе warnings[] — имена сущностей глобально уникальны.

    Типы: entityType канонизируется (синонимы RU/EN → канонический, см.
    CANONICAL_TYPES/_TYPE_SYNONYMS); свободный тип остаётся как есть. Новый bug
    получает явный status open/fixed, новый task  — open/done; без
    переданного — default "open"; status для прочих типов и невалидное значение —
    игнор с warnings[] в ответе.
    """
    with _lock:
        ents, rels = _load()
        out, changed = _create_entities_graph(ents, rels, items, project, source)
        if changed:
            _save(ents, rels)
        scope = {n for n, e in ents.items()
                 if not project or (e.get("project") or "") == project}
        _reprime_tick(project, out)
    nd = _neardup_warnings(out.get("created") or [], scope)
    if nd:
        out.setdefault("warnings", []).extend(nd)
    # подсказки к записи (similar + hygiene) — вне лока (embed =
    # HTTP к EMBED_URL из env), только совет. Тексты — факты сущности (не
    # entity_text как у neardup: name/title размывают containment) — отдельные
    # embed-вызовы, ≤_HINT_TEXTS_MAX на батч (шаринга LRU нет).
    created = out.get("created") or []
    touched = [e["name"] for e in created]
    # similar — по ФАКТАМ новой сущности (name/title — идентификаторы, в
    # containment не участвуют, иначе размывают сходство)
    new_texts = [" ".join(_obs_text(o) for o in e.get("observations", []))
                 for e in created[:_HINT_TEXTS_MAX]]
    hints = _quality_hints(ents, rels, touched, new_texts, project)
    if hints:
        out["hints"] = hints
    # авто-тайпинг (1.8.0): у созданной сущности без типа (general) — подсказка
    # по тексту; только совет, данные не трогаем. Совместимо с similar/hygiene.
    type_hints = []
    for ent in created:
        if ent.get("entityType") != "general":
            continue
        st = _suggest_type(ent.get("title"), ent.get("observations"))
        if st:
            type_hints.append({"entity": ent["name"], **st})
    if type_hints:
        out.setdefault("hints", {})["type_suggestion"] = type_hints
    return out


_NEARDUP_COS = 0.85   # слой 2: порог косинуса «похожих сущностей»; ниже — молчим
_NEARDUP_MAX = 3      # сколько кандидатов показать в одном предупреждении
_NEARDUP_CHECK_MAX = 20  # security: подсказка best-effort — за один вызов
# проверяем ≤20 новых сущностей, иначе батч из ~800 уникальных текстов выжег бы
# дневной кап embed (200k tok) для всех клиентов, а висящий эндпоинт держал бы
# запрос N×EMBED_TIMEOUT


def _neardup_warnings(created: list, scope: set) -> list:
    """Семантический near-dup новых сущностей (слой 2): при включённом embed
    вектор новой сущности — один вызов query_vector (LRU + дневной кап внутри),
    сравнение — dot() по готовым векторам с диска (без API). scope — имена,
    видимые проекту: вектора чужих проектов не читаются (изоляция, как в
    _hybrid_search). Любой сбой/кап/выключено → []: создание сущностей не
    должно падать из-за подсказки. Вызывать ВНЕ _lock (сетевой вызов)."""
    if not created:
        return []
    try:
        cfg = embed.config_from_env()
        if not embed.enabled(cfg):
            return []
        store = embed.load_store(str(EMBEDDINGS_FILE), model=cfg["model"])
        if not store:
            return []
        cands = [n for n in scope if n in store]  # security: не весь store
        if not cands:
            return []
        warnings = []
        for ent in created[:_NEARDUP_CHECK_MAX]:
            qv = embed.query_vector(embed.entity_text(ent), cfg=cfg,
                                    state_path=EMBED_STATE_FILE)
            if not qv:
                continue
            scorer = embed.cosine_scorer(store, qv)
            hits = []
            for name in cands:
                if name == ent["name"]:
                    continue
                s = scorer(name)
                if s is not None and s >= _NEARDUP_COS:
                    hits.append((s, name))
            if hits:
                hits.sort(reverse=True)
                pretty = ", ".join(f"'{n}' ({s:.2f})" for s, n in hits[:_NEARDUP_MAX])
                warnings.append(
                    f"'{ent['name']}': похоже на существующие: {pretty} — "
                    f"проверь search_nodes, прежде чем плодить дубли")
        return warnings
    except Exception as ex:
        log.warning("neardup check skipped: %s", ex)  # security: было info —
        return []                                     # поломка embed не видна


# --- подсказки качества записи ------------
# similar: ≤3 похожих записей на новые тексты (embed-косинус ≥0.92 через СВОЙ
# relay — ; embed выключен/сбой/нет вектора → FTS-фоллбэк: containment стемов
# ≥0.8, ≥3 стемов). hygiene: «15+ наблюдений — разделить?», «нет связей»,
# «нет тегов» — ≤3. ТОЛЬКО совет — граф и пульс не меняются, запись не
# блокируется, любой сбой → подсказок нет (учёт embed-квоты в embed.state.json —
# это счётчик сервиса, не память). Вызывать ВНЕ _lock (embed = HTTP).

_HINT_SIMILAR_COS = 0.92      # «почти дубликат» по эмбеддингам
_HINT_SIMILAR_CONTAIN = 0.8   # FTS-фоллбэк: доля стемов нового текста в сущности
_HINTS_MAX = 3                # не больше трёх подсказок каждого вида
_HINT_TEXTS_MAX = 5           # новых текстов на вызов проверяем (кап embed/API)
_HINT_OBS_SPLIT = 15          # «много наблюдений — разделить?»


def _similar_hints(new_texts, ents, project, self_names) -> list:
    """[{text, similar_to, title, score, via}] ≤_HINTS_MAX. Кандидаты — сущности
    видимого проекта (проект "" → полный граф, как read-пути),
    кроме записанных (self). Embed-скан — только по сущностям с векторами
    (scorer → None без вектора); FTS — по стемам из prep-кэша searchrank
    (без повторной токенизации корпуса)."""
    texts = [str(t or "").strip() for t in (new_texts or [])]
    texts = [t for t in texts if t][:_HINT_TEXTS_MAX]
    if not texts:
        return []
    try:
        cfg = embed.config_from_env()
        store = {}
        if embed.enabled(cfg):
            store = embed.load_store(str(EMBEDDINGS_FILE), model=cfg["model"])
        scope = []  # один проход: (имя, сущность-с-валидными-obs для фильтра [~дата])
        for n, e in ents.items():
            if n in self_names or (project and not _in_project(e, project)):
                continue
            obs = e.get("observations", [])
            if any(_is_invalidated(o) or not isinstance(o, str) for o in obs):
                # searchrank работает со строками — source-объекты → текст
                e = dict(e, observations=[_obs_text(o) for o in obs
                                          if not _is_invalidated(o)])
            scope.append((n, e))
        out = []
        for text in texts:
            hits = []
            qv = None
            if store:  # embed-путь: косинус по векторам с диска (без API на скан)
                # кап длины как у entity_text — длинный факт не выжигает
                # дневной кап embed (стоимость = len/4)
                qv = embed.query_vector(text[:embed.ENTITY_TEXT_MAX], cfg=cfg,
                                        state_path=EMBED_STATE_FILE)
                if qv:
                    scorer = embed.cosine_scorer(store, qv)
                    for n, _e in scope:
                        s = scorer(n)
                        if s is not None and s >= _HINT_SIMILAR_COS:
                            hits.append((s, n, "embed"))
            # FTS-фоллбэк — только когда embed-пути не было: embed
            # выключен/сбой/нет вектора; «честно пустой» embed-скан не дублируем
            if not hits and (not store or qv is None):
                qt = set(searchrank.tokenize(text))
                if len(qt) >= 3:  # <3 стемов → тривиальная 1.0, шум
                    for n, e in scope:
                        stems = searchrank._prepare(e)["stems"]
                        if stems and len(qt & stems) / len(qt) >= _HINT_SIMILAR_CONTAIN:
                            hits.append((len(qt & stems) / len(qt), n, "fts"))
            hits.sort(reverse=True)
            for s, n, via in hits:
                out.append({"text": text[:120], "similar_to": n,
                            "title": ents[n].get("title", n),
                            "score": round(s, 3), "via": via})
                if len(out) >= _HINTS_MAX:
                    return out
        return out
    except Exception as ex:
        log.warning("similar hints skipped: %s", ex)  # подсказка не роняет запись
        return []


def _hygiene_hints(ents, rels, touched_names) -> list:
    """[{entity, hint}] ≤_HINTS_MAX — механические правила гигиены графа."""
    linked = {r["from"] for r in rels} | {r["to"] for r in rels}
    out = []
    for n in touched_names:
        e = ents.get(n)
        if not e:
            continue
        nvalid = len(_valid_obs(e))
        if nvalid >= _HINT_OBS_SPLIT:
            out.append({"entity": n,
                        "hint": f"{nvalid} наблюдений — пора разделить?"})
        if n not in linked:
            out.append({"entity": n, "hint": "нет связей — привяжи create_relations"})
        if not e.get("tags"):
            out.append({"entity": n, "hint": "нет тегов"})
        if len(out) >= _HINTS_MAX:
            break
    return out[:_HINTS_MAX]


def _quality_hints(ents, rels, touched_names, new_texts, project) -> dict:
    """{"similar": [...], "hygiene": [...]} — только непустые части.
    подсказки не роняют запись — любой сбой → {} (двойная страховка над
    внутренними try _similar_hints)."""
    try:
        hints = {}
        sim = _similar_hints(new_texts, ents, project, set(touched_names))
        if sim:
            hints["similar"] = sim
        hyg = _hygiene_hints(ents, rels, touched_names)
        if hyg:
            hints["hygiene"] = hyg
        return hints
    except Exception as ex:
        log.warning("quality hints skipped: %s", ex)
        return {}


def op_update_entity(item, project: str = "", source: str = ""):
    """Update title/tags/entityType/status of an existing entity.
    project: сущность адресуема только внутри своего проекта.
    entityType канонизируется (синонимы → канонический; свободный — как есть).
    status — для bug (open/fixed) и task (open/done): для остальных типов
    и при невалидном значении — игнор с warnings[] в ответе. Смена типа на тип
    без статуса снимает status, на bug/task — ставит default "open" (явный status
    в том же вызове важнее); статус, валидный для нового типа, сохраняется."""
    with _lock:
        ents, rels = _load()
        name = item.get("name", "")
        if name not in ents or (project and not _in_project(ents[name], project)):
            suffix = f" in project '{project}'" if project else ""
            return {"error": f"Entity '{name}' not found{suffix}"}
        e = ents[name]
        warnings = []
        if item.get("title"):
            e["title"] = item["title"]
        if item.get("entityType"):
            new_type = _canonical_type(item["entityType"])
            # security-ревизия (порт 2f46408): гвард псевдо-проектов действует
            # и на смену типа — type=project только у project:<slug> СВОЕГО
            # проекта и самоссылочной сущности папки (иначе обход create-гварда
            # через retype существующей сущности)
            if new_type == "project" and name != _project_entity_name(project) \
                    and name != project:
                warnings.append(
                    f"'{name}': entityType=project зарезервирован для "
                    f"проектов-рабочих-папок — тип не изменён")
                new_type = e["entityType"]
            if new_type != e["entityType"]:
                e["entityType"] = new_type
                if new_type in _STATUS_BY_TYPE:
                    # статус, валидный для нового типа, сохраняем; иначе default open
                    if str(e.get("status") or "") not in _STATUS_BY_TYPE[new_type]:
                        e["status"] = "open"
                else:
                    e.pop("status", None)  # status живёт только на bug/task
        if item.get("tags") is not None:
            e["tags"] = list(item["tags"])
        status, serr = _check_status(item.get("status"), e["entityType"])
        if serr:
            warnings.append(f"{serr} — проигнорирован")
        elif status:
            if e["entityType"] in _STATUS_BY_TYPE:
                e["status"] = status
            else:
                warnings.append(f"status применим только к bug/task "
                                f"(тип '{e['entityType']}') — проигнорирован")
        # restore=[строки] — откат supersedes:
        # снимает маркер [~YYYY-MM-DD]. Принимает строку как с маркером, так и
        # без; ненайденные — в restoreNotFound ответа (молча не теряем).
        # Валидация типов: строка → один элемент, прочие
        # не-списки → warning.
        rest = item.get("restore")
        if rest is None:
            rest = []
        elif isinstance(rest, str):
            rest = [rest]
        elif not isinstance(rest, list):
            warnings.append("restore must be a list of strings — проигнорирован")
            rest = []
        restored, restore_nf = [], []
        for s in rest:
            s = str(s or "").strip()
            if not s:
                continue
            idx = next((i for i, o in enumerate(e["observations"])
                        if _is_invalidated(o)
                        and (_obs_text(o) == s or _obs_text(o).strip() == s
                             or _INVALIDATED_RE.sub("", _obs_text(o), count=1).strip() == s)), None)
            if idx is None:
                restore_nf.append(s)
                continue
            o = e["observations"][idx]
            unmarked = _INVALIDATED_RE.sub("", _obs_text(o), count=1)
            e["observations"][idx] = ({**o, "text": unmarked}
                                      if isinstance(o, dict) else unmarked)
            restored.append(e["observations"][idx])
        e["updatedAt"] = now_iso()
        _save(ents, rels)
        _event("update_entity", name, e["title"], project=project or (e.get("project") or ""),
               source=source)
        out = {"updated": e}
        if restored or restore_nf:
            out["restored"] = restored
            out["restoreNotFound"] = restore_nf
        if warnings:
            out["warnings"] = warnings
        _reprime_tick(project, out)
        return out


def op_create_relations(items, project: str = ""):
    with _lock:
        ents, rels = _load()
        existing = {(r["from"], r["to"], r["relationType"]) for r in rels}
        created = []
        ts = now_iso()
        for r in items:
            key = (r["from"], r["to"], r["relationType"])
            if key in existing:
                continue
            d = {"from": r["from"], "to": r["to"], "relationType": r["relationType"], "createdAt": ts}
            rels.append(d)
            existing.add(key)
            created.append(d)
            _event("create_relation", f'{r["from"]}→{r["to"]}', r["relationType"], project=project)
        if created:
            _save(ents, rels)
        out = {"created": created, "skipped": len(items) - len(created)}
        _reprime_tick(project, out)
        return out


# --- поле source наблюдения (1.6.0, recall-pack) ---
# Наблюдение — строка или {"text": str, "source": {"kind", "ref"}}. Валидация
# fail-closed на границе (доверенный MCP/REST-клиент): невалидный source —
# ошибка с индексом элемента, ничего не пишется.
_SOURCE_KINDS = ("commit", "file", "url", "issue")
_SOURCE_REF_URL_RE = re.compile(r"^https?://")
_SOURCE_REF_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _check_source(src) -> str:
    """→ "" | текст ошибки для source {"kind", "ref"}."""
    if not isinstance(src, dict):
        return "source must be an object {kind, ref}"
    kind = src.get("kind")
    if kind not in _SOURCE_KINDS:
        return f"source.kind must be one of {'|'.join(_SOURCE_KINDS)}"
    ref = src.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        return "source.ref must be a non-empty string"
    ref = ref.strip()
    if kind == "url" and not _SOURCE_REF_URL_RE.match(ref):
        return "source.ref for kind=url must start with http(s)://"
    if kind == "commit" and not _SOURCE_REF_COMMIT_RE.fullmatch(ref):
        return "source.ref for kind=commit must be a hex sha (7-40 chars)"
    if kind in ("file", "issue") and len(ref) > 500:
        return "source.ref must be ≤500 chars"
    if kind == "url" and len(ref) > 2048:
        return "source.ref for kind=url must be ≤2048 chars"
    return ""


def _check_obs_content(c) -> str:
    """Элемент contents (строка | {text, source}) → "" | текст ошибки."""
    if isinstance(c, str):
        return ""
    if not isinstance(c, dict):
        return "must be a string or {text, source}"
    if set(c) - {"text", "source"}:
        return "unknown keys (allowed: text, source)"
    if not isinstance(c.get("text"), str) or not c["text"].strip():
        return "text must be a non-empty string"
    if "source" in c:
        return _check_source(c["source"])
    return ""


def op_add_observations(items, project: str = ""):
    """project: сущность адресуема только внутри своего проекта — чужая
    сущность с тем же именем считается ненайденной (имена глобально уникальны).
    Дедуп — по _norm_obs (регистр/пробелы/краевая пунктуация), и против
    имеющихся, и внутри входящего списка; хранится оригинал строки.

    item["supersedes"] — список УСТАРЕВШИХ строк этой же сущности:
    каждая помечается маркером [~YYYY-MM-DD] и остаётся в данных (археология;
    удаление — delete_observations всегда). Совпадение — точное по тексту
    (для объектов {text, source} — по полю text, маркер ставится в text);
    уже помеченная — идемпотентный пропуск; ненайденные — в
    supersedesNotFound ответа (молча не теряем).

    contents (1.6.0): элемент — строка или {"text", "source": {kind, ref}}
    (kind ∈ commit|file|url|issue). Невалидный элемент — ValueError с индексом
    (fail-closed, граница доверия MCP/REST); REST маппит в 400."""
    today = now_iso()[:10]
    with _lock:
        ents, rels = _load()
        results, errors = [], []
        ts = now_iso()
        for item_i, item in enumerate(items):
            name, contents = item["entityName"], item.get("contents", [])
            if not isinstance(contents, list):
                raise ValueError(f"observations[{item_i}].contents must be an array")
            for ci, c in enumerate(contents):
                cerr = _check_obs_content(c)
                if cerr:
                    raise ValueError(f"observations[{item_i}].contents[{ci}]: {cerr}")
            if name not in ents or (project and not _in_project(ents[name], project)):
                suffix = f" in project '{project}'" if project else ""
                errors.append(f"Entity '{name}' not found{suffix}")
                continue
            obs_list = ents[name]["observations"]
            # инвалидация устаревших строк (до добавления новых — новый факт
            # может текстом совпадать с помечаемым старым, порядок важен).
            # Валидация типов: строка → одноимённый элемент
            # (без посимвольной итерации), прочие не-списки → error по item.
            sup = item.get("supersedes")
            if sup is None:
                sup = []
            elif isinstance(sup, str):
                sup = [sup]
            elif not isinstance(sup, list):
                errors.append(f"{name}: supersedes must be a list of strings")
                continue
            sup = [s for s in sup if str(s or "").strip()]  # пустые строки не считаются вызовом
            marked, sup_nf = [], []
            for s in sup:
                s = str(s or "").strip()
                if not s:
                    continue
                idx = next((i for i, o in enumerate(obs_list)
                            if _obs_text(o) == s or _obs_text(o).strip() == s), None)
                if idx is None:
                    # уже помечена ранее? (ищем по тексту без маркера) → идемпотентно
                    if any(_is_invalidated(o)
                           and _INVALIDATED_RE.sub("", _obs_text(o), count=1).strip() == s
                           for o in obs_list):
                        continue
                    sup_nf.append(s)
                    continue
                obs_list[idx] = _mark_observation(obs_list[idx], today)
                marked.append(s)
            if marked:
                ents[name]["updatedAt"] = ts
                _event("update_entity", name, f"supersedes ×{len(marked)}",
                       project=project or (ents[name].get("project") or ""))
            empty_call = not contents and not sup
            have = {_norm_obs(o) for o in obs_list}
            added = []
            stripped = 0
            for c in contents:
                c, was_marked = _strip_marker_on_write(c)
                stripped += 1 if was_marked else 0
                if isinstance(c, dict):
                    # храним нормализованное: валидация резала пробелы,
                    # исходник с краевыми пробелами в данных не нужен
                    c = {**c, "text": c["text"].strip()}
                    if "source" in c:
                        c = {**c, "source": {**c["source"],
                                             "ref": c["source"]["ref"].strip()}}
                k = _norm_obs(c)
                if k in have:
                    continue
                have.add(k)
                added.append(c)
            obs_list.extend(added)
            if added:
                ents[name]["updatedAt"] = ts
            r = {"entityName": name, "addedObservations": added}
            if marked or sup_nf:
                r["superseded"] = marked
                r["supersedesNotFound"] = sup_nf
            if stripped:
                r["markerStripped"] = stripped  # вход нёс [~дата] — срезали
            if empty_call:
                r["warning"] = ("пустой вызов: ни contents, ни supersedes — "
                                "записывать нечего (поле фактов называется contents)")
            results.append(r)
            for c in added:
                _event("add_observation", name, _obs_text(c)[:120],
                       project=project or (ents[name].get("project") or ""))
        if results:
            _save(ents, rels)
        out = {"results": results}
        if errors:
            out["errors"] = errors
        _reprime_tick(project, out)
        touched = [r["entityName"] for r in results]
        new_texts = [_obs_text(c) for r in results for c in r["addedObservations"]]
    # подсказки ПОСЛЕ записи, вне лока (embed = HTTP); только совет
    hints = _quality_hints(ents, rels, touched, new_texts, project)
    if hints:
        out["hints"] = hints
    return out


def op_delete_entities(names, project: str = ""):
    """project: удаляются только сущности этого проекта — чужие с тем же
    именем не трогаем (изоляция деструктивных операций)."""
    with _lock:
        ents, rels = _load()
        deleted = [n for n in names
                   if n in ents and (not project or _in_project(ents[n], project))]
        for n in deleted:
            del ents[n]
            _event("delete_entity", n, project=project)
        rels = [r for r in rels if r["from"] not in deleted and r["to"] not in deleted]
        _save(ents, rels)
        out = {"deleted": deleted}
        _reprime_tick(project, out)
        return out


def op_delete_project(project: str) -> dict:
    """Удалить проект ЦЕЛИКОМ (v1.4.7, кнопка в дашборде): все сущности с
    project=slug, структурная project:<slug> и все их связи. Пульс (events)
    не трогаем — это аудит. → {"deleted", "relations_removed"}"""
    if not project:
        return {"error": "project is required"}
    pname = _project_entity_name(project)
    with _lock:
        ents, rels = _load()
        # + сущность-проект БЕЗ префикса (агенты до протокола создавали
        # type=project с голым именем — «viewcode», «maxgraph-redesign»);
        # иначе такой проект не удалялся кнопкой («уже пуст или не найден»).
        doomed = {n for n, e in ents.items()
                  if (e.get("project") or "") == project or n == pname
                  or (n == project and e.get("entityType") == "project")}
        if not doomed:
            return {"deleted": 0, "relations_removed": 0}
        before = len(rels)
        rels = [r for r in rels
                if r.get("from") not in doomed and r.get("to") not in doomed]
        for n in doomed:
            del ents[n]
        _save(ents, rels)
        out = {"deleted": len(doomed), "relations_removed": before - len(rels)}
    _event("delete_project", project, f"{out['deleted']} entities",
           project=project)
    return out


def op_delete_observations(items, project: str = ""):
    """project: затрагиваются только сущности этого проекта."""
    with _lock:
        ents, rels = _load()
        removed = 0
        for item in items:
            name = item["entityName"]
            if name not in ents or (project and not _in_project(ents[name], project)):
                continue
            drop = set(item.get("observations", []))
            before = len(ents[name]["observations"])
            # объекты с source удаляются по тексту (1.6.0)
            ents[name]["observations"] = [o for o in ents[name]["observations"]
                                          if _obs_text(o) not in drop]
            removed += before - len(ents[name]["observations"])
            if before != len(ents[name]["observations"]):
                ents[name]["updatedAt"] = now_iso()
        _save(ents, rels)
        out = {"removed": removed}
        _reprime_tick(project, out)
        return out


def op_delete_observations_by_id(items, project: str = ""):
    """Точечный forget по id наблюдения: items — [{entity, ids}].
    id = _obs_id текста КАК ХРАНИТСЯ (см. with_ids у search_nodes/open_nodes).
    Сущность обязана существовать и быть видимой в проекте — иначе errors[]
    (как op_add_observations); ненайденные ids — в notFound записи результата
    (молча не теряем). Пульс — как у op_delete_observations: без события."""
    with _lock:
        ents, rels = _load()
        results, errors = [], []
        removed = 0
        for item in items:
            if not isinstance(item, dict):
                errors.append("item must be an object {entity, ids}")
                continue
            name = str(item.get("entity") or "").strip()
            ids = [str(i or "").strip() for i in (item.get("ids") or [])]
            ids = [i for i in dict.fromkeys(ids) if i]
            if not name:
                errors.append("item.entity is required")
                continue
            if name not in ents or (project and not _in_project(ents[name], project)):
                suffix = f" in project '{project}'" if project else ""
                errors.append(f"Entity '{name}' not found{suffix}")
                continue
            obs_list = ents[name]["observations"]
            have = {_obs_id(o) for o in obs_list}
            deleted = [i for i in ids if i in have]
            not_found = [i for i in ids if i not in have]
            if deleted:
                drop = set(deleted)
                ents[name]["observations"] = [o for o in obs_list
                                              if _obs_id(o) not in drop]
                ents[name]["updatedAt"] = now_iso()
                removed += len(obs_list) - len(ents[name]["observations"])
            results.append({"entity": name, "deleted": deleted,
                            "notFound": not_found})
        if results:
            _save(ents, rels)
        out = {"removed": removed, "results": results}
        if errors:
            out["errors"] = errors
        _reprime_tick(project, out)
        return out


def op_delete_relations(items, project: str = ""):
    """project: удаляются только связи, чьи ОБА конца видимы в проекте —
    кросс-проектные связи из контекста проекта не трогаем."""
    with _lock:
        ents, rels = _load()
        drop = {(r["from"], r["to"], r["relationType"]) for r in items}
        visible = None
        if project:
            visible = {n for n, e in ents.items() if _in_project(e, project)}
        before = len(rels)
        rels = [r for r in rels
                if (r["from"], r["to"], r["relationType"]) not in drop
                or (visible is not None and (r["from"] not in visible or r["to"] not in visible))]
        _save(ents, rels)
        out = {"removed": before - len(rels)}
        _reprime_tick(project, out)
        return out


def op_read_graph(project: str = "", all_projects: bool = False, raw_obs: bool = False):
    with _lock:
        ents, rels = _load()
    es, rs = _filter_graph(ents, rels, project, all_projects)
    # объекты с source (1.6.0) отдаются текстом; источник — с with_ids=true
    # у search_nodes/open_nodes; raw_obs=True (REST /api/graph — дашборд
    # рендерит source-чипы) сохраняет объекты как есть
    if not raw_obs:
        es = [dict(e, observations=[_obs_text(o) for o in e.get("observations", [])])
              if any(not isinstance(o, str) for o in e.get("observations", []))
              else e for e in es]
    return {"entities": es, "relations": rs}


def _hybrid_search(query_str: str):
    """(vector_weight, cosine_fn, vector_top_fn) для searchrank.rank;
    (0.0, None, None) — чистый FTS.

    Гибрид применяется ТОЛЬКО когда embed включён (EMBED_URL+EMBED_KEY) И у
    запроса есть вектор (LRU-кэш в embed; промах = один /embeddings вызов с
    EMBED_TIMEOUT; сбой/кап → None). Любой сбой → (0.0, None, None):
    поведение без изменений. Корпус уже отфильтрован ДО вызова rank —
    косинусы считаются только по именам из видимого корпуса (изоляция
    проектов сохраняется, вектора чужих проектов не читаются).
    vector_top_fn(m) — имена грубого векторного топ-M по центроидам
    (embed.scan_top; двухуровневый скан для корпуса > HYBRID_FULL_SCAN_MAX)."""
    cfg = embed.config_from_env()
    if not embed.enabled(cfg):
        return 0.0, None, None
    w = cfg["hybrid_weight"]
    if w <= 0.0:
        return 0.0, None, None  # w=0 → чистый FTS: запрос даже не эмбеддим (не жжём API)
    store = embed.load_store(EMBEDDINGS_FILE)  # stat-only при тёплом кэше
    if not store:
        return 0.0, None, None  # пополнитель ещё не наполнил — не жжём API на запрос
    qv = embed.query_vector(query_str, cfg=cfg, state_path=EMBED_STATE_FILE)
    if qv is None:
        return 0.0, None, None

    def vector_top_fn(m):
        return [nm for nm, _cos in embed.scan_top(store, qv, m)]

    return w, embed.cosine_scorer(store, qv), vector_top_fn


# --- PRF (Семантика 2.0): векторный топ → стемы заголовков/имён → FTS --------
_PRF_TOP_K = 5         # столько верхних по векторному скору сущностей дают стемы
_PRF_COARSE_POOL = 50  # верх грубого центроидного пула для точного дожима PRF
_PRF_MAX_TERMS = 8     # потолок дописываемых в FTS стемов (шум размывает BM25)
_PRF_MIN_COS = 0.5     # гейт: шумовой максимум cos на 10k×384d ≈ 0.2–0.25,
                       # релевантные e5-пары ≈ 0.7–0.9 (bench/results) —
                       # 0.5 консервативная середина; подтверждено офлайн-бенчем
                       # (онлайн-доводка — bench-ключом, не pro-ключом)
_PRF_FTS_MIN_HITS = 3  # меньше результатов — FTS слаб, PRF нужен
_PRF_FTS_CONFIDENCE = 2.0  # top1/median FTS-скоров ≥ 2 → FTS уверен, PRF
                       # пропускаем: дописанные стемы сдвигают idf/BM25 и
                       # РАЗБАВЛЯЮТ FTS-сильную выдачу (регрессы бенча
                       # 2026-10-21 #7/#8: top1/med 5.0/3.5; спасённые PRF
                       # кейсы — flat 1.2)


def _prf_expand(query_str, corpus, cos_fn, vtop_fn):
    """Pseudo-relevance feedback: векторный топ-K → стемы title/name, которых
    нет в стемах запроса, дописываются в FTS-ветку (источник — вектора,
    не словарь синонимов). Стеммер идемпотентен на своих выходах — re-tokenize
    расширенного запроса вернёт те же стемы.

    Грубый центроидный пул (vtop_fn — обычно уже посчитанный для rank) →
    точный max-по-окнам cos_fn только по пулу → топ-K с cos ≥ _PRF_MIN_COS.
    Без векторов/при сбое — запрос не меняется (no-op, fail-open)."""
    if vtop_fn is None or cos_fn is None:
        return query_str
    try:
        names = vtop_fn(_PRF_COARSE_POOL)
    except Exception:
        return query_str  # fail-open: PRF не роняет поиск
    if not names:
        return query_str
    by_name = {str(e.get("name") or ""): e for e in corpus}
    qstems = set(searchrank.tokenize(query_str))
    hits = []
    for nm in names:
        e = by_name.get(str(nm))
        if e is None:
            continue
        try:
            cos = cos_fn(str(nm))
        except Exception:
            cos = None  # fail-open, как в rank
        if cos is None or cos < _PRF_MIN_COS:
            continue
        hits.append((float(cos), e))
    hits.sort(key=lambda t: t[0], reverse=True)
    extra = []
    for _cos, e in hits[:_PRF_TOP_K]:
        for stem in (searchrank.tokenize(str(e.get("title") or ""))
                     + searchrank.tokenize(str(e.get("name") or ""))):
            if stem not in qstems and stem not in extra:
                extra.append(stem)
                if len(extra) >= _PRF_MAX_TERMS:
                    break
        if len(extra) >= _PRF_MAX_TERMS:
            break
    return query_str + " " + " ".join(extra) if extra else query_str


# --- temporal-фильтры поиска (1.6.0, recall-pack) ---
# У наблюдений своих timestamp нет — фильтр entity-level по createdAt/updatedAt.
# Формат дат хранилища фиксирован (now_iso, UTC) → сравнение строкового
# префикса YYYY-MM-DD корректно; «to» — конец дня включительно.

def _check_date(value) -> str:
    """Граница даты YYYY-MM-DD → строка | "" (мусор и невалидные даты молча
    игнорируются — фильтр best-effort, не 400)."""
    s = str(value or "").strip()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s):
        return ""
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return ""
    return s


def _date_in(iso, dfrom: str, dto: str) -> bool:
    """createdAt/updatedAt ∈ [dfrom, dto] по префиксу даты; пустые границы не
    ограничивают. Сущность без даты (легаси) в датированный диапазон не
    попадает — «когда» у неё неизвестно."""
    if not dfrom and not dto:
        return True
    d = str(iso or "")[:10]
    return bool(d) and (not dfrom or d >= dfrom) and (not dto or d <= dto)


# поле по умолчанию — updatedAt («когда трогали»); слова создания → createdAt
_TEMPORAL_CREATED_RE = re.compile(
    r"создан\w*|создал\w*|добавлен\w*|добавил\w*|\bcreated\b|\badded\b",
    re.IGNORECASE)
# стемы месяцев RU (первые 3 буквы; «мае/май/мая/маю» целиком) и EN → номер
_MONTHS3 = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мае": 5, "май": 5,
            "мая": 5, "маю": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9,
            "окт": 10, "ноя": 11, "дек": 12, "jan": 1, "feb": 2, "mar": 3,
            "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
            "oct": 10, "nov": 11, "dec": 12}
# «в <месяц> [год]» / «in <month> [year]»; формы — полные слова (не стемы,
# иначе «в маршруте» совпадало бы с мартом)
_TEMPORAL_MONTH_RE = re.compile(
    r"\b(?:в|in)\s+(январ[ьяею]|феврал[ьяею]|март[аеу]?|апрел[ьяею]|ма[йяею]|"
    r"июн[ьяею]|июл[ьяею]|август[аеу]?|сентябр[ьяею]|октябр[ьяею]|ноябр[ьяею]|"
    r"декабр[ьяею]|january|jan|february|feb|march|mar|april|apr|may|june|jun|"
    r"july|jul|august|aug|september|sept|sep|october|oct|november|nov|"
    r"december|dec)\b(?:\s+(\d{4}))?", re.IGNORECASE)


def _months_ago(d, n: int):
    """Дата d минус n календарных месяцев (день клэмпится по длине месяца)."""
    m = d.month - n
    y = d.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def _parse_temporal(query, now=None):
    """Временна́я фраза RU/EN в запросе → (field, from_iso, to_iso,
    cleaned_query, phrase) | None (нераспознанное не трогаем, мусор и дикие
    числа не ломают — fail-open).

    Покрытие: сегодня/today, вчера/yesterday, на этой/прошлой неделе, this/last
    week, в этом/прошлом месяце, this/last month, за последние N дней|недель|
    месяцев, last N days|weeks|months, «в <месяц> [год]» / in <month> [year].
    field — updatedAt по умолчанию; «создан/добавлен/created/added» → createdAt.
    Диапазоны — даты ISO; «месяц без года» про прошедший месяц → прошлый год."""
    q = str(query or "")
    if not q.strip():
        return None
    now = now or datetime.now(timezone.utc)
    today = now.date()
    field = "createdAt" if _TEMPORAL_CREATED_RE.search(q) else "updatedAt"

    def hit(span, dfrom, dto):
        cleaned = re.sub(r"\s+", " ", q[:span[0]] + " " + q[span[1]:]).strip()
        return (field, dfrom.isoformat(), dto.isoformat(), cleaned,
                q[span[0]:span[1]])

    try:
        m = re.search(r"\b(?:сегодня|today)\b", q, re.IGNORECASE)
        if m:
            return hit(m.span(), today, today)
        m = re.search(r"\b(?:вчера|yesterday)\b", q, re.IGNORECASE)
        if m:
            d = today - timedelta(days=1)
            return hit(m.span(), d, d)
        m = re.search(r"на\s+этой\s+неделе|\bthis\s+week\b", q, re.IGNORECASE)
        if m:
            return hit(m.span(), today - timedelta(days=today.weekday()), today)
        m = re.search(r"на\s+прошлой\s+неделе|\blast\s+week\b", q, re.IGNORECASE)
        if m:
            mon = today - timedelta(days=today.weekday() + 7)
            return hit(m.span(), mon, mon + timedelta(days=6))
        m = re.search(r"в\s+этом\s+месяце|\bthis\s+month\b", q, re.IGNORECASE)
        if m:
            return hit(m.span(), today.replace(day=1), today)
        m = re.search(r"в\s+прошлом\s+месяце|\blast\s+month\b", q, re.IGNORECASE)
        if m:
            last = today.replace(day=1) - timedelta(days=1)
            return hit(m.span(), last.replace(day=1), last)
        m = re.search(r"за\s+последн\w*\s+(\d+)\s+(день|дня|дней|недел\w+|месяц\w*)\b",
                      q, re.IGNORECASE)
        if not m:
            m = re.search(r"\blast\s+(\d+)\s+(days?|weeks?|months?)\b",
                          q, re.IGNORECASE)
        if m:
            n, unit = int(m.group(1)), m.group(2).lower()
            if unit.startswith(("д", "day")):
                dfrom = today - timedelta(days=n)
            elif unit.startswith(("н", "week")):
                dfrom = today - timedelta(weeks=n)
            else:
                dfrom = _months_ago(today, n)
            return hit(m.span(), dfrom, today)
        m = _TEMPORAL_MONTH_RE.search(q)
        if m:
            month = _MONTHS3[m.group(1).lower()[:3]]
            year = (int(m.group(2)) if m.group(2)
                    else now.year - (1 if month > now.month else 0))
            dfrom = date(year, month, 1)
            dto = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
            return hit(m.span(), dfrom, dto)
    except (ValueError, OverflowError):  # дикий N/год — фразу игнорируем
        return None
    return None


def op_search(query, project: str = "", all_projects: bool = False, type_filter: str = "",
              limit: int = searchrank.DEFAULT_LIMIT, with_ids: bool = False,
              created_from: str = "", created_to: str = "",
              updated_from: str = "", updated_to: str = "",
              raw_obs: bool = False, include_cold: bool = False):
    """Поиск. Непустой query — ранжированный FTS-lite (searchrank): BM25-подобный
    скор по полям name×3/title×2/tags×1.5/observations×1 + фразовый бонус + recency ≤10%,
    топ-limit (default 20); у сущностей ответа + score, snippet, highlights (позиции
    совпадений в snippet — без HTML, безопасно для esc()). Ранжирование идёт внутри
    видимого корпуса: фильтры project/all_projects, type= (после нормализации)
    и даты (ниже) комбинируются как фильтры корпуса ДО скоринга.

    Temporal (1.6.0): created_from/created_to/updated_from/updated_to — даты
    YYYY-MM-DD (включительно; to = конец дня; невалидные молча игнорируются).
    Без явных параметров запрос прогоняется через _parse_temporal (RU/EN-фразы;
    фраза вынимается из FTS-запроса); сработавший парсер → meta.temporal
    {phrase, from, to, field} в ответе. Явные параметры перебивают парсер.

    Опционально: при заданных EMBED_URL+EMBED_KEY — гибрид FTS+косинус
    эмбеддингов (searchrank.rank(vector_weight=HYBRID_VECTOR_WEIGHT, default 0.6);
    корпус ≤6000 → косинус по всем (full-scan), больше → двухуровневый скан:
    FTS top-200 + грубый векторный топ-200 по центроидам;
    score ответа становится blended 0..1). PRF (Семантика 2.0): векторный топ-5
    с cos≥0.5 дописывает стемы title/name вне запроса в FTS-ветку (≤8 стемов);
    без векторов — no-op.
    Без эмбеддингов (выключен/сбой/нет векторов) — чистый FTS.

    ACT-R буст (default ON, отключается ACTR_MAX_BOOST=0) — часто и недавно
    читаемые сущности получают множитель ≤ 1+ACTR_MAX_BOOST (default +25%) к
    итоговому скору (FTS или blended); top-20 выдачи учитывается как обращение
    (_touch). Счётчики глобальны по имени, но имена глобально уникальны и буст
    применяется после фильтра корпуса — изоляция проектов сохраняется.

    Пустой query — старое поведение: все сущности (с фильтрами), без score-полей,
    без лимита. Query без извлекаемых терминов (пунктуация, CJK — вне алфавита
    токенизатора) — фоллбек на старое подстрочное совпадение (без score-полей).

    Сущности ответа несут канонический type; bug — ещё и status (явный, а для legacy
    без поля — выведенный эвристикой _bug_is_open); task — status
    (явный open/done, битое/отсутствующее хранимое значение → "open").

    with_ids: observations сущностей ответа — объекты {id, text, source?}
    (id = _obs_id, для delete_observations_by_id); default False — старый
    формат (список строк; объекты с source отдаются текстом, источник виден
    только с with_ids=true).

    include_cold (1.9.0, default False): cold-сущности (без обращений
    COLD_AFTER_DAYS+ дней) исключены из корпуса до скоринга; True — вернуть
    их в выдачу (попавшие в top-20 проходят обычный _touch → revive)."""
    tf = _canonical_type(type_filter) if str(type_filter or "").strip() else ""
    query_str = str(query or "")
    cfrom, cto = _check_date(created_from), _check_date(created_to)
    ufrom, uto = _check_date(updated_from), _check_date(updated_to)
    temporal_meta = None
    if not (cfrom or cto or ufrom or uto):
        # явные фильтры дат перебивают парсер: фраза в запросе тогда — просто текст
        parsed = _parse_temporal(query_str)
        if parsed is not None:
            field, pfrom, pto, query_str, phrase = parsed
            if field == "createdAt":
                cfrom, cto = pfrom, pto
            else:
                ufrom, uto = pfrom, pto
            temporal_meta = {"phrase": phrase, "from": pfrom, "to": pto,
                             "field": field}
    with _lock:
        ents, rels = _load()
    corpus = list(ents.values())
    if project and not all_projects:
        corpus = [e for e in corpus if _in_project(e, project)]
    if tf:
        corpus = [e for e in corpus if _canonical_type(e.get("entityType")) == tf]
    if cfrom or cto or ufrom or uto:
        corpus = [e for e in corpus
                  if _date_in(e.get("createdAt"), cfrom, cto)
                  and _date_in(e.get("updatedAt"), ufrom, uto)]
    if not include_cold:
        # cold (1.9.0) уходят из корпуса ДО скоринга; include_cold возвращает
        # их в выдачу — и под обычный _touch top-20 (revive бесплатно)
        now_ts = time.time()
        corpus = [e for e in corpus if not _is_cold(e, now_ts)]
    # инвалидированные ([~дата]) наблюдения исключаются из поиска —
    # ни из скоринга (FTS), ни из сниппета, ни из полей ответа (устаревшая цена
    # не должна всплывать). Полная правда с маркерами — read_graph/open_nodes.
    # Векторная составляющая гибрида — по последнему embed-снимку (маркер
    # попадёт туда при ближайшем refresh после updatedAt-бампа).
    # Поисковый корпус — только текст (searchrank считает по строкам);
    # исходный вид (объекты с source, 1.6.0) сохраняем для полей ответа.
    _corpus, orig_obs = [], {}
    for e in corpus:
        obs = e.get("observations", [])
        if any(_is_invalidated(o) or not isinstance(o, str) for o in obs):
            orig_obs[e["name"]] = [o for o in obs if not _is_invalidated(o)]
            _corpus.append(dict(e, observations=[_obs_text(o)
                                                 for o in orig_obs[e["name"]]]))
        else:
            _corpus.append(e)
    corpus = _corpus
    # FTS-ветка расширяется PRF (векторный топ → стемы title/name — ниже,
    # после _hybrid_search); векторный запрос остаётся исходным.
    expanded_str = query_str
    qts = searchrank.tokenize(expanded_str)
    if query_str.strip() and qts:
        vec_w, cos_fn, vtop_fn = _hybrid_search(query_str)  # (0.0, None, None) → чистый FTS
        if cos_fn is not None and vtop_fn is not None:
            # Гейт по силе FTS (дешёвый пробный прогон): PRF — для запросов,
            # где лексика молчит или выдача плоская (top1/med < 2); при
            # уверенном FTS дописанные стемы только разбавляют выдачу.
            fts_probe = searchrank.rank(corpus, expanded_str, limit=5)
            _prf_ok = True
            if len(fts_probe) >= _PRF_FTS_MIN_HITS:
                _med = sorted(s for s, _ in fts_probe)[len(fts_probe) // 2]
                _prf_ok = not _med or fts_probe[0][0] / _med < _PRF_FTS_CONFIDENCE
            if _prf_ok:
                # Один грубый центроидный проход на PRF и двухуровневый скан rank
                # (иначе 10k платил бы за скан дважды). rank потребляет только
                # имена и только при n > HYBRID_FULL_SCAN_MAX.
                try:
                    _coarse = vtop_fn(max(_PRF_COARSE_POOL,
                                          searchrank.HYBRID_CENTROID_TOP))
                except Exception:
                    _coarse = None  # fail-open: rank сам повторит и отвалится молча
                if _coarse is not None:
                    vtop_fn = lambda m, _c=list(_coarse): _c[:m]
                    prf_str = _prf_expand(expanded_str, corpus, cos_fn, vtop_fn)
                    if prf_str != expanded_str:
                        expanded_str = prf_str
                        qts = searchrank.tokenize(expanded_str)
        pairs = searchrank.rank(corpus, expanded_str, limit=limit,
                                vector_weight=vec_w, cosine_fn=cos_fn,
                                actr_fn=_actr_boost_fn(),  # None → без изменений
                                vector_top_fn=vtop_fn)     # None → без двухуровневого
        ranked = True
    else:
        ranked = False
        if query_str.strip():
            # термины не извлеклись (CJK/пунктуация) — фоллбек на подстрочный поиск
            ql = query_str.lower()
            corpus = [e for e in corpus
                      if ql in e["name"].lower()
                      or ql in e.get("title", "").lower()
                      or ql in e["entityType"].lower()
                      or any(ql in t.lower() for t in e.get("tags", []))
                      or any(ql in o.lower() for o in e["observations"])]
        pairs = [(0.0, e) for e in corpus]
    names = {e["name"] for _, e in pairs}
    mrels = [r for r in rels if r["from"] in names and r["to"] in names]
    out_entities = []
    for s, e in pairs:
        v = dict(e)
        v["type"] = _canonical_type(e.get("entityType"))
        if v["type"] == "bug":
            # контракт ответа — open|fixed: невалидное хранимое значение (ручная
            # правка файла) не отдаём наружу, выводим эвристикой как для legacy
            st = str(e.get("status") or "").strip().lower()
            v["status"] = st if st in BUG_STATUSES else ("open" if _bug_is_open(e) else "fixed")
        elif v["type"] == "task":
            # контракт ответа — open|done; задачи через границу записи всегда
            # имеют явный status (default open), прочее → «не закрыта»
            v["status"] = _task_status_out(e)
        if ranked:
            v["score"] = round(s, 4)
            text, highlights = searchrank.snippet(e, qts)
            v["snippet"] = text
            v["highlights"] = highlights
        # исходный вид observations (объекты с source) — из orig_obs, не из
        # поисковой строковой копии
        if with_ids:  # {id, text, source?} (id от текста как хранится)
            v["observations"] = _obs_with_ids(orig_obs.get(e["name"],
                                                           v.get("observations", [])))
        elif e["name"] in orig_obs:
            # raw_obs (REST /api/search → дашборд): объекты с source как есть;
            # MCP по умолчанию — текст (обратная совместимость)
            v["observations"] = (list(orig_obs[e["name"]]) if raw_obs
                                 else [_obs_text(o) for o in orig_obs[e["name"]]])
        out_entities.append(v)
    if query_str.strip():
        # «осмысленное» чтение — top-20 выдачи (ranked и CJK-фоллбек).
        # Пустой query (дамп корпуса) не учитываем — как read_graph, это не
        # сигнал интереса к конкретным фактам (и это O(N) касаний на вызов).
        _touch([e["name"] for e in out_entities[:actr.TOUCH_TOP]])
    out = {"entities": out_entities, "relations": mrels}
    if temporal_meta is not None:
        out["meta"] = {"temporal": temporal_meta}
    return out


def op_open(names, project: str = "", with_ids: bool = False):
    """with_ids: observations — объекты {id, text, source?} (id = _obs_id,
    для delete_observations_by_id); default False — старый формат строк
    (объекты с source отдаются текстом, 1.6.0)."""
    want = set(names)
    with _lock:
        ents, rels = _load()
    if not project:
        # как раньше: связи — оба конца среди запрошенных имён (даже если узла нет)
        found = [e for n, e in ents.items() if n in want]
        frels = [r for r in rels if r["from"] in want and r["to"] in want]
        _touch([e["name"] for e in found])  # явное открытие = обращение
    else:
        found = [e for n, e in ents.items() if n in want and _in_project(e, project)]
        found_names = {e["name"] for e in found}
        frels = [r for r in rels if r["from"] in found_names and r["to"] in found_names]
        _touch([e["name"] for e in found])  # только реально найденные имена
    if with_ids:  # копии — хранимый граф не трогаем
        found = [dict(e, observations=_obs_with_ids(e.get("observations", [])))
                 for e in found]
    else:
        found = [dict(e, observations=[_obs_text(o)
                                       for o in e.get("observations", [])])
                 if any(not isinstance(o, str) for o in e.get("observations", []))
                 else e for e in found]
    return {"entities": found, "relations": frels}


def op_timeline(limit=100, project: str = "", all_projects: bool = False):
    with _lock:
        if not EVENTS_FILE.exists():
            return {"events": []}
        lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    events = []
    if project and not all_projects:
        # фильтр до лимита: последние `limit` событий ИМЕННО этого проекта
        for line in reversed(lines):
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("project") == project:
                events.append(ev)
                if len(events) >= limit:
                    break
        return {"events": events}
    for line in reversed(lines[-limit:]):
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    return {"events": events}


# ---------------------------------------------------- work_state -----

# (аналог work_state у claude-mem): открытые дела проекта — сущности
# entityType "task" со status open/done (default open). Имя — task-<slug(title)>
# (транслит RU→lat, kebab); коллизия (имена глобально уникальны) → суффикс -2, -3…
_TASK_NAME_MAX = 64   # потолок длины name целиком (включая префикс «task-»)
_TASK_TITLE_MAX = 200  # title задачи — однострочник (как дайджест хуков)
_TASK_OPEN_TASKS_MAX = 20  # SessionEnd data.open_tasks: максимум пунктов за раз

# RU→lat для slug'ов имён задач (title задачи — обычно по-русски)
_TASK_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
_TASK_SLUG_BAD = re.compile(r"[^a-z0-9]+")


def _task_slug(title) -> str:
    """title задачи → kebab-slug [a-z0-9-] (транслит RU, нижний регистр)."""
    s = "".join(_TASK_TRANSLIT.get(ch, ch) for ch in str(title or "").strip().lower())
    return _TASK_SLUG_BAD.sub("-", s).strip("-")


def _task_name_free(ents: dict, title: str, taken=()) -> str:
    """Свободное имя task-<slug(title)>: коллизия → суффикс -2, -3… (суффикс
    гарантированно влезает в _TASK_NAME_MAX). Работает на уже загруженном графе
    (caller держит лок и сам пишет _save) — для батчей /api/ingest; taken — имена,
    уже зарезервированные внутри текущего батча (в ents их ещё нет)."""
    slug = _task_slug(title)
    base = (f"task-{slug}" if slug else "task")[:_TASK_NAME_MAX]
    name, n = base, 2
    while name in ents or name in taken:
        suffix = f"-{n}"
        name = base[:_TASK_NAME_MAX - len(suffix)] + suffix
        n += 1
    return name


def _task_is_open(e: dict) -> bool:
    """Открыта ли задача: status != done. Задачи через границу записи несут явный
    status (default open); битое/отсутствующее значение ≠ done → открыта («никто
    не закрыл — открыта», как у legacy-багов)."""
    return str(e.get("status") or "").strip().lower() != "done"


def _task_status_out(e: dict) -> str:
    """status задачи для ответов наружу (open|done): хранимый валидный, иначе
    «не закрыта» (open) — контракт как у bug (см. op_search)."""
    st = str(e.get("status") or "").strip().lower()
    return st if st in TASK_STATUSES else "open"


def op_task_add(title, note: str = "", project: str = "", source: str = "",
                links=()) -> dict:
    """Создать задачу: entityType task, status open, имя task-<slug(title)>
    (коллизия → -2…), note — первый факт (опционально). Проектная семантика
    (автосоздание project:<slug>, part-of) — из _create_entities_graph.
    links=[имена] — связи «relates to» к существующим сущностям
    своего проекта в том же вызове (контекст задачи не теряется); неизвестные/
    чужие — в links_skipped (молча не теряем).
    → {"created": entity, "linked": [...], "links_skipped": [...]} | {"error": …}."""
    title = re.sub(r"\s+", " ", str(title or "")).strip()[:_TASK_TITLE_MAX]
    if not title:
        return {"error": "task_add: пустой title"}
    note = str(note or "").strip()
    with _lock:
        ents, rels = _load()
        item = {"name": _task_name_free(ents, title), "title": title,
                "entityType": "task", "observations": [note] if note else []}
        out, changed = _create_entities_graph(ents, rels, [item], project, source)
        if not out.get("created"):
            # имя только что было свободно → создание гарантировано; сюда не доходим
            result = {"error": f"task для «{title}» не создана"}
        else:
            result = {"created": out["created"][0]}
            if out.get("warnings"):
                result["warnings"] = out["warnings"]
            # связи в том же вызове (только внутри проекта)
            linked, skipped = [], []
            rel_keys = {(r["from"], r["to"], r["relationType"]) for r in rels}
            for l in (links or []):
                l = str(l or "").strip()
                if not l or l == item["name"]:
                    continue
                if l in ents and _in_project(ents[l], project):
                    key = (item["name"], l, "relates to")
                    if key not in rel_keys:
                        rels.append({"from": item["name"], "to": l,
                                     "relationType": "relates to",
                                     "createdAt": now_iso()})
                        rel_keys.add(key)
                        linked.append(l)
                else:
                    skipped.append(l)
            if linked:
                changed = True
                result["linked"] = linked
            if skipped:
                result["links_skipped"] = skipped
        if changed:
            _save(ents, rels)
        _reprime_tick(project, result)
    return result


def op_task_done(name: str, project: str = "", source: str = "") -> dict:
    """Закрыть задачу: status=done + событие task_done в пульс.
    Только entityType=task своего проекта: отсутствующая/чужая → not found,
    не-task → error. Уже закрытая — идемпотентно {"updated", "already": True}
    без повторного события.
    При закрытии — связь «discussed» от session-сущности сегодняшнего
    дня к задаче, если сессия дня существует (stub SessionEnd-хука тоже считается;
    новую сессию НЕ создаём — авто-записей нет). Ответ: + linked_to_session."""
    with _lock:
        ents, rels = _load()
        e = ents.get(name)
        if e is None or (project and not _in_project(e, project)):
            suffix = f" in project '{project}'" if project else ""
            return {"error": f"Task '{name}' not found{suffix}"}
        if _canonical_type(e.get("entityType")) != "task":
            return {"error": f"'{name}' — не задача (тип '{e.get('entityType')}')"}
        if not _task_is_open(e):
            out = {"updated": e, "already": True}
            _reprime_tick(project, out)
            return out
        e["status"] = "done"
        e["updatedAt"] = now_iso()
        # связь к сессии дня (вечерняя запись «что сделано» знает свои задачи)
        proj = project or (e.get("project") or "")
        sname, _closed = _today_session(ents, proj, now_iso()[:10])
        if sname:
            key = (sname, name, "discussed")
            if not any((r["from"], r["to"], r["relationType"]) == key for r in rels):
                rels.append({"from": sname, "to": name, "relationType": "discussed",
                             "createdAt": now_iso()})
        _save(ents, rels)
        out = {"updated": e}
        if sname:
            out["linked_to_session"] = sname
        _reprime_tick(project, out)
    _event("task_done", name, e["title"], project=project or (e.get("project") or ""),
           source=source)
    return out


def op_task_list(project: str = "") -> dict:
    """Открытые задачи (status≠done), recency-first. project задан — только его
    задачи; без project — все открытые задачи графа (плоское чтение, как
    read_graph без проекта). → {"tasks": [...], "total": N}.
    Контракт чтения: task_list — «полная правда», как
    read_graph/open_nodes: инвалидированные наблюдения видны с маркером
    [~дата] (фильтруют только session_start и search)."""
    with _lock:
        ents, _rels = _load()
    tasks = sorted((e for e in ents.values()
                    if e.get("entityType") == "task" and _task_is_open(e)
                    and (not project or _in_project(e, project))),
                   key=_recency_key, reverse=True)
    out = []
    for e in tasks:
        v = dict(e)
        v["type"] = "task"
        v["status"] = _task_status_out(e)
        out.append(v)
    return {"tasks": out, "total": len(out)}


# --- global-слой (1.7.0): promote/demote — единственный путь смены project ---
# Обычная запись в global (create_entities с project="global") не запрещается:
# «запись в global только явная» и значит «явная».

def _read_shard_entity(name: str, slug: str):
    """Сырая entity-запись name из шард-файла slug ("" = плоский memory.json)
    или None. Нужна promote/demote для коллизии имени между шард-файлами:
    _load держит одну запись на имя (имена глобально уникальны, последняя
    выигрывает), дубль в другом шарде виден только на диске. Whitelist-сборка
    и last-wins — как в _load: подпорченный шард не проносит лишних ключей
    и не-списковых observations в граф."""
    path = MEMORY_FILE if not slug else PROJECTS_DIR / f"{slug}.json"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    found = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "entity" or rec.get("name") != name:
            continue
        obs = rec.get("observations")
        found = {
            "name": name,
            "title": rec.get("title") or name,
            "entityType": rec.get("entityType", "general"),
            "tags": list(rec.get("tags", [])) if isinstance(rec.get("tags"), list) else [],
            "observations": obs if isinstance(obs, list) else [],
            "createdAt": rec.get("createdAt", ""),
            "updatedAt": rec.get("updatedAt", rec.get("createdAt", "")),
        }
        if rec.get("status"):
            found["status"] = rec["status"]
    return found


def _merge_obs(dst: dict, src_obs: list) -> int:
    """Долить наблюдения src_obs в dst с дедупом по _norm_obs (строки и объекты
    {text, source} 1.6.0 — по тексту, как merge в create_entities). → добавлено."""
    have = {_norm_obs(o) for o in dst.get("observations", [])}
    added = 0
    for o in src_obs:
        k = _norm_obs(o)
        if k in have:
            continue
        have.add(k)
        dst.setdefault("observations", []).append(o)
        added += 1
    return added


def _move_entity_project(name: str, dst: str, src: str, action: str,
                         source: str = "", ev_project: str = "") -> dict:
    """Ядро promote/demote: смена поля project сущности + updatedAt + событие
    пульса (detail=name, метка ev_project — у promote исходный проект, у demote
    принимающий). Наблюдения/теги/тип не трогаются, связи — тоже (from/to без
    проекта; кросс-проектные связи видны в all_projects-вью). Физический
    переезд между projects/<a>.json и projects/global.json делает _save: шард
    сущности = её поле project (_shard_of_entity).
    src — обязательный текущий проект ("" = любой, кроме dst). Дубль имени
    в шарде назначения → merge наблюдений, ответ {"merged": True}."""
    name = str(name or "").strip()
    if not name:
        return {"error": "пустое имя сущности"}
    if src and src == dst:
        return {"error": f"'{name}': источник и назначение совпадают ('{dst}')"}
    if name.startswith("project:"):
        # структурный маркер project:<slug> привязан к своему проекту — перенос
        # травил бы блок «# Проект» в session_start обоих проектов
        return {"error": f"'{name}' — структурный маркер проекта, не переносится"}
    with _lock:
        ents, rels = _load()
        e = ents.get(name)
        if e is None:
            return {"error": f"Entity '{name}' not found"}
        cur = e.get("project") or ""
        ts = now_iso()
        if cur == dst:
            if not src:
                return {"error": f"Entity '{name}' уже в проекте '{dst or 'плоском графе'}'"}
            dup = _read_shard_entity(name, src)
            if dup is None:
                return {"error": f"Entity '{name}' не в проекте '{src}' "
                                 f"(она в '{cur or 'плоском графе'}')"}
            # в памяти выиграла dst-версия, переносимая — дубль в шарде src;
            # доливаем её наблюдения, дубль пропадает при перезаписи шарда
            added = _merge_obs(e, dup.get("observations", []))
            e["updatedAt"] = ts
            _save(ents, rels)
            out = {"merged": True, "entity": e, "observations_added": added}
            lost = [f for f in ("title", "tags", "entityType", "status")
                    if dup.get(f) is not None and dup.get(f) != e.get(f)]
            if lost:
                out["warnings"] = [f"merge: метаданные дубля ({', '.join(lost)}) "
                                   f"не перенесены — долиты только наблюдения"]
        elif src and cur != src:
            return {"error": f"Entity '{name}' не в проекте '{src}' "
                             f"(она в '{cur or 'плоском графе'}')"}
        else:
            dup = _read_shard_entity(name, dst)
            if dup is not None:
                # коллизия имени в назначении: merge в существующую запись,
                # переносимая исчезает из исходного проекта
                added = _merge_obs(dup, e.get("observations", []))
                dup["project"] = dst
                dup["updatedAt"] = ts
                ents[name] = dup
                _save(ents, rels)
                out = {"merged": True, "entity": dup, "observations_added": added}
                lost = [f for f in ("title", "tags", "entityType", "status")
                        if e.get(f) is not None and e.get(f) != dup.get(f)]
                if lost:
                    out["warnings"] = [f"merge: метаданные переносимой записи "
                                       f"({', '.join(lost)}) не перенесены — "
                                       f"долиты только наблюдения"]
            else:
                e["project"] = dst
                e["updatedAt"] = ts
                _save(ents, rels)
                out = {"moved": e}
        _event(action, name, name, project=ev_project or src or dst, source=source)
        _reprime_tick(ev_project or src or dst, out)
        return out


def op_promote_global(name: str, project: str = "", source: str = "") -> dict:
    """Перенос сущности в спецпроект global: её decision-факты попадают в
    session_start любого проекта. project — где сущность сейчас ("" = любой).
    Коллизия имени в global → merge ({"merged": true}), исходная исчезает."""
    return _move_entity_project(name, GLOBAL_PROJECT, project, "promote_global", source)


def op_demote_global(name: str, project: str, source: str = "") -> dict:
    """Обратный перенос из global в указанный проект (project обязателен)."""
    if not project:
        return {"error": "demote требует project=<kebab-slug> назначения"}
    return _move_entity_project(name, project, GLOBAL_PROJECT, "demote_global",
                                source, ev_project=project)


# --- закрытие сессии одним вызовом + напоминание --
# session_close создаёт/мерджит session-сущность дня с итогами
# и discussed-связями за один вызов (вместо 3 забываемых), а session_start
# напоминает о незакрытой сессии. Инварианты: напоминание и
# авто-сбор связей из пульса — МЕХАНИЧЕСКИЕ (без LLM) и НИЧЕГО не пишут за
# агента (напоминание — только строка в context_block; авто-линки — лишь внутри
# явного вызова session_close самим агентом).

_SESSION_CLOSE_TAG = "session-close"  # «сессия закрыта агентом с итогами»
_SESSION_LINKS_MAX = 20               # потолок discussed-связей за вызов
_UNCLOSED_MIN_WRITES = 5              # напоминание при ≥5 пишущих событиях за день
_UNCLOSED_TOUCHED_MAX = 5             # сколько имён затронутых показать
_WRITE_ACTIONS = frozenset({          # пишущие события пульса (для авто-линков)
    "create_entity", "merge_entity", "create_relation", "update_entity",
    "add_observation", "delete_entity", "task_done", "session_close",
    "promote_global", "demote_global",
})

# Детектор «пустой сессии» (1.8.0): активная работа по хукам за день, а
# пишущих событий в граф — 0 → session_close честно предупреждает (не блокирует).
EMPTY_SESSION_MIN_EVENTS = 5
_EMPTY_SESSION_ACTIVITY = frozenset({"hook_PostToolUse", "hook_UserPromptSubmit"})
_EMPTY_SESSION_WRITES = frozenset({"add_observation", "create_entity", "update_entity"})


# --- слой 2 «устойчивость к компактизации»: reprime_hint ----------------------
# In-memory счётчик пишущих op-вызовов с последнего session_start проекта:
# ≥5 → в ответ пишущей операции добавляется reprime_hint (совет перечитать
# контекст — защита от потери деталей после компактизации/длинной сессии).
# Без пульса и без диска: рестарт сервера = чистый счётчик (агент по протоколу
# и так делает session_start на старте). Хук-трафик не считается — api_ingest
# идёт мимо op_* (через ядро _create_entities_graph).
_REPRIME_MIN_WRITES = 5
_reprime_writes = {}


def _reprime_tick(project: str, out: dict) -> None:
    """Вызывать под _lock в конце пишущих op_*: инкремент счётчика и,
    при достижении порога, добавление reprime_hint в ответ. Проект без
    session_start (нет записи) и плоский граф ("") не подсказываются."""
    if not project:
        return
    n = _reprime_writes.get(project)
    if n is None:
        return
    n += 1
    _reprime_writes[project] = n
    if n >= _REPRIME_MIN_WRITES:
        out["reprime_hint"] = (
            f"после последнего session_start — {n} пишущих операций: если "
            f"контекст сессии мог сжаться, вызови session_start("
            f"project='{project}') заново")


def _today_session(ents: dict, project: str, today: str):
    """(name, closed): session-сущность проекта, созданная сегодня (stub от
    SessionEnd-хука или session_close — одна на день+проект), и закрыта ли она
    агентом с итогами (тег session-close)."""
    name, closed = "", False
    for e in ents.values():
        if e.get("entityType") != "session":
            continue
        if (e.get("project") or "") != project:
            continue
        if str(e.get("createdAt", ""))[:10] != today:
            continue
        name = e["name"]
        if _SESSION_CLOSE_TAG in (e.get("tags") or []):
            closed = True
    return name, closed


def _touched_today(project: str, today: str, exclude: str = "", lines=None) -> list:
    """Имена сущностей из сегодняшних пишущих событий проекта (пульс), свежие
    первыми, ≤10. Чтение журнала — механика без LLM; здесь ничего не пишется.
    lines — уже прочитанный events.jsonl (переиспользование без повторного I/O);
    None → прочитать файл самим."""
    if lines is None:
        if not EVENTS_FILE.exists():
            return []
        lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    names, seen = [], set()
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if (ev.get("project") or "") != project:
            continue
        if str(ev.get("ts", ""))[:10] != today:
            continue
        if ev.get("action") not in _WRITE_ACTIONS:
            continue
        ent = str(ev.get("entity") or "")
        if not ent or ent == exclude or ent in seen:
            continue
        seen.add(ent)
        names.append(ent)
        if len(names) >= 10:
            break
    return names


def op_session_close(summary: str = "", done=(), links=(), project: str = "",
                     source: str = "") -> dict:
    """Закрыть сессию дня одним вызовом: session-сущность дня (merge в уже
    созданную сегодня, в т.ч. в stub от SessionEnd-хука) + summary/done как
    наблюдения + discussed-связи. links пуст → авто-сбор из сегодняшнего пульса
    (механически, по пишущим событиям). Повторный вызов за день — домердж.
    → {"session", "merged", "linked", "links_skipped", "open_tasks_hint",
       "already_closed"} | {"error": …} (пустые summary+done)."""
    summary = re.sub(r"\s+", " ", str(summary or "")).strip()
    obs = []
    if summary:
        obs.append(summary)
    for d in (done or []):
        d = re.sub(r"\s+", " ", str(d or "")).strip()
        if d:
            obs.append(d)
    if not obs:
        return {"error": "session_close: пусто — дай summary или done"}
    today = now_iso()[:10]
    # пульс читаем ДО лока (один раз, свежести на секунды для связей достаточно) —
    # чтобы не держать _lock на чтении журнала
    ev_lines = (EVENTS_FILE.read_text(encoding="utf-8").splitlines()
                if EVENTS_FILE.exists() else [])
    raw_touched = _touched_today(project, today, lines=ev_lines)
    with _lock:
        ents, rels = _load()
        name, already = _today_session(ents, project, today)
        merged = bool(name)
        changed = False
        if merged:
            e = ents[name]
            have = {_obs_text(o) for o in e["observations"]}
            added = [o for o in obs if o not in have]
            if added:
                e["observations"].extend(added)
                changed = True
            if _SESSION_CLOSE_TAG not in (e.get("tags") or []):
                e.setdefault("tags", []).append(_SESSION_CLOSE_TAG)
                changed = True
            if changed:
                e["updatedAt"] = now_iso()
        else:
            name = f"session-{today}-" + (project or "flat")
            base, n = name, 2
            while name in ents:  # имена глобально уникальны → суффикс
                name = f"{base}-{n}"
                n += 1
            item = {"name": name, "entityType": "session",
                    "title": f"Сессия {today}" + (f" ({project})" if project else ""),
                    "tags": [_SESSION_CLOSE_TAG], "observations": obs}
            _out, changed = _create_entities_graph(ents, rels, [item], project, source)
        touched = [t for t in raw_touched
                   if t in ents and t != name and t != _project_entity_name(project)]
        if links:
            want, skipped = [], []
            for l in links:
                l = str(l or "").strip()
                if not l or l == name:
                    continue
                # явные связи тоже внутри проекта
                if l in ents and _in_project(ents[l], project):
                    want.append(l)
                else:
                    skipped.append(l)
            auto = False
        else:
            want, skipped, auto = touched, [], True
        if len(want) > _SESSION_LINKS_MAX:  # хвост не теряем молча
            skipped.extend(want[_SESSION_LINKS_MAX:])
            want = want[:_SESSION_LINKS_MAX]
        existing = {(r["from"], r["to"], r["relationType"]) for r in rels}
        linked = []
        for l in want:
            key = (name, l, "discussed")
            if key in existing:
                continue
            rels.append({"from": name, "to": l, "relationType": "discussed",
                         "createdAt": now_iso()})
            existing.add(key)
            linked.append(l)
        hint = [t for t in touched
                if ents[t].get("entityType") == "task" and _task_is_open(ents[t])]
        # пустой повторный вызов (ничего не добавилось) — без _save и без
        # события в пульс, ответ идемпотентный
        if changed or linked:
            _save(ents, rels)
        out = {"session": name, "merged": merged, "already_closed": already,
               "linked": linked, "links_auto": auto, "links_skipped": skipped,
               "open_tasks_hint": hint}
        _reprime_tick(project, out)
    if changed or linked:
        _event("session_close", name, obs[0][:120], project=project, source=source)
    # детектор «пустой сессии» (1.8.0) — по пульсу дня, прочитанному до лока:
    # хук-активность ≥ порога, а записей в граф 0 → warning (закрытию не мешает)
    activity = writes = 0
    for line in ev_lines:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if (ev.get("project") or "") != project or str(ev.get("ts", ""))[:10] != today:
            continue
        act = ev.get("action")
        if act in _EMPTY_SESSION_ACTIVITY:
            activity += 1
        elif act in _EMPTY_SESSION_WRITES:
            writes += 1
    if activity >= EMPTY_SESSION_MIN_EVENTS and not writes:
        out["warning"] = (f"сессия закрыта без записей в память при активной "
                          f"работе ({activity} событий хуков)")
        _event("empty_session_warn", name, f"activity={activity}", project=project)
    return out


def _open_tasks_from_data(data) -> list:
    """SessionEnd data.open_tasks → нормализованные title'ы. Ввод
    недоверенный (хук): только строки, whitespace схлопнут, ≤_TASK_TITLE_MAX,
    пустые/дубли отброшены, ≤_TASK_OPEN_TASKS_MAX пунктов."""
    raw = (data or {}).get("open_tasks")
    if not isinstance(raw, list):
        return []
    out = []
    for t in raw:
        if not isinstance(t, str):
            continue
        t = re.sub(r"\s+", " ", t).strip()[:_TASK_TITLE_MAX]
        if t and t not in out:
            out.append(t)
        if len(out) >= _TASK_OPEN_TASKS_MAX:
            break
    return out


# ------------------------------------------------------ session_start --

# бюджетная инъекция контекста проекта — аналог
# SessionStart-блока claude-mem (~50 наблюдений + ~10 сессий, жёсткий бюджет
# ~10k chars). Операция чтения: событие в пульс НЕ пишем, лог-строку — да.
SESSION_START_BUDGET = 10000   # default budget_chars
_SS_CAP_PROJECT_FACTS = 5      # 3-5 ключевых фактов сущности project:<slug>
_SS_CAP_DECISION_LINES = 50    # строк наблюдений decisions (дословно)
_SS_CAP_BUG_LINES = 30         # строк открытых багов
_SS_CAP_TASK_LINES = 10        # строк открытых задач
_SS_CAP_SESSIONS = 10          # прошлых сессий
_SS_CAP_EVENTS = 15            # событий пульса
# Запас под маркер усечения: «… (ещё N, усечено бюджетом)\n» ≤ 32 chars, т.к.
# N ≤ сумме кэпов (120); берём с запасом — иначе маркер мог бы превысить бюджет.
_SS_MARKER_RESERVE = 40

# Эвристика статуса бага — ФОЛЛБЕК для legacy-багов без явного поля status:
# последнее статусное слово в фактах («status: fixed», «закрыт»…) или тег
# (closed / status:open). Новые баги несут явный status open/fixed.
_BUG_STATUS_WORD = re.compile(
    r"\b(open|reopened|fixed|closed|resolved|done|открыт\w*|закрыт\w*|исправлен\w*|пофикш\w*)\b",
    re.IGNORECASE)
_BUG_OPEN_WORDS = {"open", "reopened"}


def _bug_word_is_open(word: str) -> bool:
    return word in _BUG_OPEN_WORDS or word.startswith("открыт")


def _bug_is_open(e: dict) -> bool:
    """Открыт ли баг. ЯВНЫЙ status (open/fixed) решает первым. Legacy-баг без
    status (или с неизвестным значением) — эвристика: закрыт, если тег
    (closed/fixed/status:closed…) или ПОСЛЕДНЕЕ статусное слово в фактах —
    закрывающее. Без сигналов баг считается открытым: никто не закрыл — открыт."""
    st = str(e.get("status") or "").strip().lower()
    if st in BUG_STATUSES:
        return st == "open"
    for t in e.get("tags", []):
        word = str(t).split(":", 1)[-1].strip().lower()
        if _bug_word_is_open(word):
            return True
        if _BUG_STATUS_WORD.fullmatch(word):
            return False
    last = ""
    for obs in e.get("observations", []):
        for m in _BUG_STATUS_WORD.finditer(_obs_text(obs)):
            last = m.group(1).lower()
    if not last:
        return True
    return _bug_word_is_open(last)


def _recency_key(e: dict):
    """Recency-first: updatedAt (ISO, сортируется строково), tie-break — имя."""
    return (e.get("updatedAt") or e.get("createdAt") or "", e["name"])


def _ss_ts(e: dict) -> float:
    """updatedAt|createdAt → epoch seconds; битое/пустое → 0.0 (нейтрально,
    без штрафа: такая сущность просто окажется внизу recency-сортировки)."""
    try:
        dt = datetime.fromisoformat(str(e.get("updatedAt") or e.get("createdAt") or ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _ss_sort_recent(entities, mult_fn=None):
    """Recency-first сортировка секций session_start. Без mult_fn — ровно старое
    поведение (_recency_key, byte-identical). С mult_fn (ACT-R) ключ —
    ts × множитель: часто читаемая и чуть более старая сущность обгоняет свежую
    нетронутую (множитель ≤ 1+ACTR_MAX_BOOST, т.е. перекрывает разрыв ≤25%
    по свежести — большие разрывы во времени остаются решающими)."""
    if mult_fn is None:
        return sorted(entities, key=_recency_key, reverse=True)

    def key(e):
        try:
            m = float(mult_fn(e["name"]))
        except Exception:  # fail-open: буст не роняет session_start
            m = 1.0
        if not 0.0 < m < float("inf"):  # NaN/inf/≤0 → нейтраль
            m = 1.0
        return (_ss_ts(e) * m, e["name"])

    return sorted(entities, key=key, reverse=True)


def _ss_head(e: dict, field: str = "title") -> str:
    """Общий заголовок строки секции: «- **<title|name>** (YYYY-MM-DD)»."""
    date = (e.get("updatedAt") or "")[:10]
    return f"- **{e[field]}**" + (f" ({date})" if date else "")


def _fit_budget(sections: list, budget: int):
    """Сборка context_block под ЖЁСТКИЙ бюджет: len(блока) никогда > budget.

    sections — [(key, header, body_lines)] в порядке приоритета; обрезка идёт
    снизу вверх (последняя секция жертвуется первой), построчно — строка не
    рвётся посередине. При усечении в конец блока добавляется маркер
    «… (ещё N, усечено бюджетом)», N — число не влезших контентных строк.
    → (text, shown_lines_per_section, truncated)
    """
    def blen(lines) -> int:
        return sum(len(x) for x in lines) + max(0, len(lines) - 1)

    full = []
    for _key, header, body in sections:
        full.append(header)
        full.extend(body)
    if blen(full) <= budget:
        return "\n".join(full), [len(body) for _k, _h, body in sections], False

    out, shown, dropped, cutting = [], [], 0, False
    for _key, header, body in sections:
        if cutting or blen(out + [header]) > budget - _SS_MARKER_RESERVE:
            cutting = True  # секция целиком жертвуется (и все ниже)
            dropped += len(body) + 1  # +1 — заголовок секции тоже не влез
            shown.append(0)
            continue
        out.append(header)
        n = 0
        for line in body:
            if blen(out + [line]) <= budget - _SS_MARKER_RESERVE:
                out.append(line)
                n += 1
            else:
                cutting = True
                dropped += len(body) - n
                break
        shown.append(n)
    marker = f"… (ещё {dropped}, усечено бюджетом)"
    if blen(out + [marker]) <= budget:
        # гарантировано reserve'ом при любом непустом out (marker ≤ 31 < reserve)
        out.append(marker)
    elif not out and len(marker) <= budget:
        out = [marker]  # микро-бюджет: влез только маркер
    text = "\n".join(out)
    if len(text) > budget:  # даже заголовок/маркер не влезли — честный пустой блок
        text, shown = "", [0] * len(sections)
    return text, shown, True


def op_session_start(project: str, budget_chars: int = SESSION_START_BUDGET):
    """Бюджетная инъекция контекста проекта: {context_block, meta}.

    Секции по приоритету (обрезка бюджетом — снизу вверх, от последней; порядок
    жертвования: events → sessions → global → unclosed → tasks → bugs → decisions):
      # Проект: <slug>    — title + до 5 ключевых фактов project:<slug>
      ## Решения владельца — entityType=decision, recency-first, дословно (≤50 строк)
      ## Открытые баги    — entityType=bug, явный status≠fixed; legacy без
                            status — эвристика тега/статусного слова (≤30 строк)
      ## Открытые задачи  — entityType=task, status≠done (work_state, ≤10
                            строк): title + первый факт; дела важнее истории,
                            поэтому выше сессий
      ## Глобальная память — decision-сущности спецпроекта global (1.7.0), те же
                            recency × ACT-R; баги/задачи global не подтягиваются
                            (project-scoped по смыслу). В project="global"
                            секция не дублируется; нет global-decision — нет секции
      ## Незакрытая сессия — (1 строка, только если ≥5 пишущих событий
                            сегодня и сессия дня без тега session-close);
                            механическое напоминание, ничего не пишет
      ## Прошлые сессии   — entityType=session, recency-first (≤10): имя + первые 2 факта
      ## Недавние события — последние ≤15 событий пульса проекта
    Пустые секции (кроме первой) опускаются; пустой проект → блок-подсказка
    «начни с create_entities». meta.sections[key] = {items, total}: items —
    строк/единиц секции в блоке, total — всего кандидатов в графе; расхождение
    без truncated=true означает срабатывание кэпа секции, а не бюджета.

    ACT-R (default ON; ACTR_MAX_BOOST=0 → выкл., byte-identical):
    внутри секций decisions/bugs/tasks сортировка — recency × множитель
    частоты обращений (≤+25%); сущности, попавшие в итоговый context_block
    (после бюджетной обрезки), учитываются как обращения (_touch). Сессии —
    чистая хронология (свежесть сессии важна сама по себе), секция событий —
    журнал, не чтение фактов сущности: ни буста, ни учёта.

    Cold (1.9.0): сущности без обращений COLD_AFTER_DAYS+ дней (default 90,
    0=выкл) исключаются из ВСЕХ секций, включая global; meta.cold.skipped —
    сколько спрятано (ключ есть только при skipped>0).
    """
    try:
        budget = max(0, int(budget_chars))
    except (TypeError, ValueError):
        budget = SESSION_START_BUDGET
    pname = _project_entity_name(project)
    mult_fn = _actr_boost_fn()  # None → сортировки без изменений
    with _lock:
        ents, _rels = _load()
        ev_lines = (EVENTS_FILE.read_text(encoding="utf-8").splitlines()
                    if EVENTS_FILE.exists() else [])
        _reprime_writes[project] = 0  # слой 2: свежий прайм — счётчик с нуля
    proj_ent = ents.get(pname)
    now_ts = time.time()
    proj_all = [e for e in ents.values() if (e.get("project") or "") == project]
    # cold (1.9.0): невостребованные COLD_AFTER_DAYS+ дней уходят из всех
    # секций; проверка «проект пуст» ниже — по proj_all (полностью
    # cold-проект ≠ новый проект, подсказка create_entities была бы ложной)
    proj_ents = [e for e in proj_all if not _is_cold(e, now_ts)]
    cold_skipped = len(proj_all) - len(proj_ents)
    # инвалидированные ([~дата]) строки в контекст не попадают
    proj_facts = _valid_obs(proj_ent) if proj_ent else []

    # события проекта: один проход файла — счёт + последние N (кольцевой буфер)
    # + счётчик СЕГОДНЯШНИХ пишущих событий для напоминания
    # «незакрытая сессия» (чтение журнала, ничего не пишется).
    today = now_iso()[:10]
    writes_today = 0
    recent_buf = deque(maxlen=_SS_CAP_EVENTS)
    total_events = 0
    for line in ev_lines:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("project") == project:
            total_events += 1
            recent_buf.append(ev)
            if (str(ev.get("ts", ""))[:10] == today
                    and ev.get("action") in _WRITE_ACTIONS):
                writes_today += 1

    title = (proj_ent or {}).get("title") or project
    header = f"# Проект: {project}" + (f" — {title}" if title != project else "")
    totals = {"project": len(proj_facts), "decisions": 0, "bugs": 0,
              "tasks": 0, "sessions": 0, "events": total_events,
              "unclosed": 0}  # 1 — напоминание «незакрытая сессия» показано
    sections = []  # (key, header, lines) — только секции с контентом (+ всегда project)
    sec_names = []  # имя сущности каждой строки sections ("" = без сущности)

    if not proj_all and not proj_facts and not total_events:
        notice = (f"Проект «{project}» новый или пуст: в памяти по этому slug пока ничего нет. "
                  f'Начни с create_entities(project="{project}") — сущность проекта создастся '
                  "автоматически, факты добавляй через add_observations. Этот блок наполнится "
                  "после первых записей.")
        totals["project"] = 1  # единственная «строка контента» — сама подсказка
        sections.append(("project", header, [notice]))
        sec_names.append([""])  # подсказка — не сущность, обращения нет
    else:
        if proj_ent is not None and _is_cold(proj_ent, now_ts):
            # факты cold project:<slug> не показываем; заголовок — структурный
            proj_facts = []
            totals["project"] = 0  # total — по показанному, иначе выглядит как кэп
            cold_skipped += 1
        plines = [f"- {_obs_text(f)}" for f in proj_facts[:_SS_CAP_PROJECT_FACTS]]
        sections.append(("project", header, plines))
        sec_names.append([pname] * len(plines))  # факты — это строки project:<slug>

        # внутри decisions/bugs/tasks — recency × ACT-R множитель
        # (mult_fn=None → ровно старый recency-first).
        decisions = _ss_sort_recent(
            (e for e in proj_ents if e.get("entityType") == "decision"), mult_fn)
        dlines, dnames = [], []
        for e in decisions:
            obs_valid = _valid_obs(e)  # инвалидированные решения не показываем
            totals["decisions"] += len(obs_valid)
            prefix = _ss_head(e) + ": "
            for obs in obs_valid:
                if len(dlines) >= _SS_CAP_DECISION_LINES:
                    break
                dlines.append(prefix + _obs_text(obs))
                dnames.append(e["name"])
            if len(dlines) >= _SS_CAP_DECISION_LINES:
                break
        if dlines:
            sections.append(("decisions", "## Решения владельца", dlines))
            sec_names.append(dnames)

        bugs = _ss_sort_recent(
            (e for e in proj_ents
             if e.get("entityType") == "bug" and _bug_is_open(e)), mult_fn)
        blines, bnames = [], []
        for e in bugs:
            obs_valid = _valid_obs(e)
            totals["bugs"] += max(1, len(obs_valid))
            head = _ss_head(e)
            if not obs_valid:
                if len(blines) < _SS_CAP_BUG_LINES:
                    blines.append(head)  # имя открытого бага — уже сигнал
                    bnames.append(e["name"])
                continue
            for obs in obs_valid:
                if len(blines) >= _SS_CAP_BUG_LINES:
                    break
                blines.append(head + ": " + _obs_text(obs))
                bnames.append(e["name"])
            if len(blines) >= _SS_CAP_BUG_LINES:
                break
        if blines:
            sections.append(("bugs", "## Открытые баги", blines))
            sec_names.append(bnames)

        # (work_state): открытые задачи — после багов, перед сессиями
        # (дела важнее истории); бюджетом режутся после сессий, до багов.
        tasks = _ss_sort_recent(
            (e for e in proj_ents
             if e.get("entityType") == "task" and _task_is_open(e)), mult_fn)
        totals["tasks"] = len(tasks)
        tlines, tnames = [], []
        for e in tasks[:_SS_CAP_TASK_LINES]:
            line = _ss_head(e)
            obs_valid = _valid_obs(e)  # первый ВАЛИДНЫЙ факт как note
            if obs_valid:
                line += ": " + _obs_text(obs_valid[0])
            tlines.append(line)
            tnames.append(e["name"])
        if tlines:
            sections.append(("tasks", "## Открытые задачи", tlines))
            sec_names.append(tnames)

        # «незакрытая сессия» — сегодня были пишущие события, но
        # session-сущности дня с итогами (тег session-close) нет. Только строка
        # в context_block: НИЧЕГО не записывается (строгий инвариант). Секция —
        # про сегодняшние дела, поэтому выше истории сессий.
        if writes_today >= _UNCLOSED_MIN_WRITES:
            _sname, sclosed = _today_session(ents, project, today)
            if not sclosed:
                touched = [t for t in _touched_today(project, today, lines=ev_lines)
                           if t in ents][:_UNCLOSED_TOUCHED_MAX]
                line = (f"Сегодня работа шла ({writes_today} пишущих событий), но "
                        f"сессия не закрыта — закрой через session_close(summary, "
                        f"done=[...]). Затронуты: {', '.join(touched) or '—'}. "
                        f"(механическое напоминание из пульса, ничего не записано)")
                sections.append(("unclosed", "## Незакрытая сессия", [line]))
                sec_names.append([""])  # напоминание — не чтение сущности
                totals["unclosed"] = 1

        # Сессии — чистая хронология (без ACT-R): свежесть сессии важна сама
        # по себе, а не частотой обращений.
        sessions = sorted((e for e in proj_ents if e.get("entityType") == "session"),
                          key=_recency_key, reverse=True)
        totals["sessions"] = len(sessions)
        slines, snames = [], []
        for e in sessions[:_SS_CAP_SESSIONS]:
            line = _ss_head(e, "name")
            facts2 = [_obs_text(o) for o in _valid_obs(e)[:2]]
            if facts2:
                line += ": " + "; ".join(facts2)
            slines.append(line)
            snames.append(e["name"])
        if slines:
            sections.append(("sessions", "## Прошлые сессии", slines))
            sec_names.append(snames)

        elines = []
        for ev in reversed(recent_buf):  # новые первыми, как list_timeline
            ts = str(ev.get("ts", ""))[:16].replace("T", " ")
            line = f"- {ts} {ev.get('action', '')}".rstrip()
            ent = str(ev.get("entity") or "")
            if ent:
                line += f" {ent}"
            det = str(ev.get("detail") or "")
            # detail add_observation несёт сырой
            # текст факта — инвалидированная строка не должна жить в контексте
            # через журнал; действие/сущность/время остаются
            if det and ev.get("action") != "add_observation":
                line += f" — {det}"
            elines.append(line)
        if elines:
            sections.append(("events", "## Недавние события", elines))
            sec_names.append([""] * len(elines))  # журнал — не чтение фактов

    # Глобальная память (1.7.0): после проектных decisions/bugs/tasks, до
    # «Прошлые сессии»/«Недавние события» — так порядок жертвования бюджетом
    # выходит events → sessions → global → … (локальный контекст важнее).
    if project != GLOBAL_PROJECT:
        gdec_all = [e for e in ents.values()
                    if (e.get("project") or "") == GLOBAL_PROJECT
                    and e.get("entityType") == "decision"]
        gdec = _ss_sort_recent((e for e in gdec_all if not _is_cold(e, now_ts)),
                               mult_fn)
        cold_skipped += len(gdec_all) - len(gdec)
        glines, gnames, gtotal = [], [], 0
        for e in gdec:
            obs_valid = _valid_obs(e)
            gtotal += len(obs_valid)
            prefix = _ss_head(e) + ": "
            for obs in obs_valid:
                if len(glines) >= _SS_CAP_DECISION_LINES:
                    break
                glines.append(prefix + _obs_text(obs))
                gnames.append(e["name"])
            if len(glines) >= _SS_CAP_DECISION_LINES:
                break
        if gtotal:
            totals["global"] = gtotal  # ключа нет, когда global-проекта/decision нет
        if glines:
            pos = next((i for i, (k, _, _) in enumerate(sections)
                        if k in ("sessions", "events")), len(sections))
            sections.insert(pos, ("global", "## Глобальная память", glines))
            sec_names.insert(pos, gnames)

    text, shown, truncated = _fit_budget(sections, budget)
    # обращениями считаются сущности, реально попавшие в context_block
    # (после бюджетной обрезки — shown[i] строк секции i, порядок = порядок
    # строк). Пульс НЕ пишется (операция чтения, как и раньше).
    touched, seen = [], set()
    for (_k, _h, _b), line_names, nshow in zip(sections, sec_names, shown):
        for nm in line_names[:nshow]:
            if nm and nm not in seen:
                seen.add(nm)
                touched.append(nm)
    if touched:
        _touch(touched)
    sec_meta = {k: {"items": 0, "total": t} for k, t in totals.items()}
    for (key, _h, _b), n in zip(sections, shown):
        sec_meta[key]["items"] = n
    meta = {"project": project, "budget_chars": budget, "used_chars": len(text),
            "sections": sec_meta, "truncated": truncated}
    if cold_skipped:
        meta["cold"] = {"skipped": cold_skipped}  # честность: сколько спрятано
    log.info("session_start: project=%s budget=%d used=%d truncated=%s",
             project, budget, len(text), truncated)
    return {"context_block": text, "meta": meta}


# --------------------------------------------------------------- schemas ----

# Входные модели MCP-инструментов: extra="forbid" — опечатка в имени поля
# даёт громкую ошибку валидации, а не тихую потерю данных (pydantic default
# extra="ignore" молча выбрасывал неизвестные поля).

class EntityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    entityType: str = "general"
    title: str = ""
    tags: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    status: str = ""  # только bug (open/fixed) и task (open/done), default open


class RelationIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    from_: str = Field(alias="from")
    to: str
    relationType: str


class ObservationsIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    entityName: str
    # копипаст с create_entities/delete_observations приносит поле observations —
    # принимаем оба имени (в схеме для хостов остаётся contents)
    # 1.6.0: элемент — строка или {"text": str, "source": {"kind", "ref"}}
    contents: list[str | dict] = Field(default_factory=list,
                                       validation_alias=AliasChoices("contents", "observations"))
    # устаревшие строки этой сущности — пометить [~дата], не удалять
    supersedes: list[str] = Field(default_factory=list)


class DeletionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entityName: str
    observations: list[str] = Field(default_factory=list)


class ObsIdDeletionIn(BaseModel):
    """forget по id — {entity, ids} (id = _obs_id из выдачи with_ids)."""
    model_config = ConfigDict(extra="forbid")
    entity: str
    ids: list[str] = Field(default_factory=list)


# ------------------------------------------------------------- licensing ---

def _license_status() -> dict:
    """licensing.status с fail-closed при внутренней ошибке + свежая версия магазина."""
    try:
        st = licensing.status(DATA_DIR, LICENSE_SECRET)
        latest = licensing.latest_version(DATA_DIR, LICENSE_SECRET)
        st["latest"] = latest
        st["update_available"] = licensing.version_newer(latest, VERSION)
        st["fp_class"] = hostfp.fp_class()  # дашборд: баннер непривязанной установки
        return st
    except Exception as e:  # статус не читается — не доверяем
        log.error("licensing.status failed: %s", e)
        return {"state": "expired", "days_left": 0, "reason": f"status error: {e}"}


def _write_block():
    """None — писать можно; dict — мутации заблокированы (trial expired).

    Чтение, /api/graph, /api/search, /api/timeline, /api/stats, /api/export,
    /healthz — НЕ блокируются никогда (продуктовое обещание «экспорт всегда»).
    """
    st = _license_status()
    if licensing.allows_write(st):
        return None
    log.warning("write blocked: license state=%s (%s)", st.get("state"), st.get("reason", ""))
    return {"error": "Trial expired — activate license: /#activate", "activateUrl": "/#activate",
            "license": st.get("state")}


def _readonly_guard():
    """None — писать можно; dict — инстанс READONLY-зеркало: мутации ПАМЯТИ запрещены.

    Зеркало получает memory.json ресинком с прода — локальные записи стирались бы
    молча, поэтому отклоняем явно: REST → HTTP 403, MCP → {"ok": false, ...}.
    Биллинг/лицензии сюда не входят: они живут на проде, зеркало их не обслуживает,
    но и не блокирует (см. спеку: readonly касается только памяти).
    """
    if not READONLY:
        return None
    return {"ok": False, "error": "readonly_mirror"}


# ------------------------------------------------------------------ MCP -----

# stateless_http=True: без session-id → рестарт сервера не убивает клиентов
# (иначе клиенты ловят -32600 "Session not found" до перезапуска клиента).
_MCP_INSTRUCTIONS = """You have persistent long-term memory (this server). Use it actively, every session:

SESSION START: call session_start(project=<slug>) — a budgeted context block (project facts, owner decisions, open bugs, open tasks, past sessions, recent events; never exceeds budget_chars). Use read_graph only when you need the full graph, or search_nodes for a specific topic / when the project slug is unknown.
AFTER CONTEXT COMPRESSION: if your harness replaced earlier dialogue with an automatic summary/checkpoint, call session_start(project=<slug>) again before continuing — the graph is unaffected by compression, and write-tool responses carry a reprime_hint when a fresh read is due.
WRITE IMMEDIATELY, never batch to the end of the conversation:
- new project/service/file/tool/person → create_entities (name = kebab-case id forever, title = human-readable; search_nodes first, never duplicate);
- new durable fact about an existing entity → add_observations (one observation = one atomic fact, dated when it matters);
- X uses/depends on Y → create_relations in active voice ("uses", "depends on", "configured by" — not "is used by");
- status/name change → add_observations with date, or update_entity for title/tags/type; bug fixed → update_entity(status="fixed"); task done → task_done;
- a fact became STALE (old price, superseded decision) → add_observations(supersedes=[exact old text]) — marks it [~date], hides from search/session_start, keeps in archive; undo → update_entity(restore=[...]); hard delete is always available (delete_observations).
WORK STATE (open loops): something to do later → task_add(project, title, note); it lands in session_start «Открытые задачи» until task_done(project, name); current open list — task_list(project).
TYPES: canonical — project/session/bug/decision/request/event/task (synonyms RU/EN are canonicalized automatically; free-form types still work). bug has explicit status open/fixed, task — open/done (default open); search_nodes(type=…) filters by type, bug/task answers carry status.
PROJECTS: if your host reports a project slug (kebab-case, e.g. from git remote/root/cwd), pass it as `project` in EVERY call — writes, reads and searches stay isolated per project; all_projects=true is an explicit cross-project search. No project param = flat shared graph (legacy default).
SESSION END: call session_close(project, summary, done=[...], links=[...]) — ONE call: creates/merges today's session entity with outcomes and links it "discussed" to touched entities (links empty → derived mechanically from today's pulse). This replaces manual create_entities + create_relations.
GLOBAL: knowledge shared by ALL projects (user rules, skills, people) lives in the special project "global" — its decision entities appear in session_start of any project («## Глобальная память»). Move an entity there ONLY on explicit user intent: promote_to_global(name); move back with demote_from_global(name, project).

RU: у тебя есть постоянная память. Старт сессии — session_start(project=<slug>) — бюджетный блок контекста (read_graph — если нужен весь граф, search_nodes — по теме); после сжатия контекста (чекпоинт/summary от харнесса) — сразу повтори session_start; факты пиши СРАЗУ (create_entities/add_observations/create_relations, связи в активном залоге, name=kebab-case навсегда); конец сессии — session_close(project, summary, done, links) одним вызовом. Полный протокол: docs/PROTOCOL.md."""

mcp = FastMCP("maxgraph", host="0.0.0.0", port=PORT, streamable_http_path="/mcp",
              stateless_http=True, instructions=_MCP_INSTRUCTIONS)
# FastMCP (mcp 1.x) НЕ принимает version= и не пробрасывает её в низкоуровневый
# Server → serverInfo.version при initialize отдавал версию MCP SDK (покупатель
# видел "" вместо версии продукта). Выставляем версию продукта напрямую.
try:
    mcp._mcp_server.version = VERSION
except Exception:  # стабы в тестах (FastMCP-заглушка без _mcp_server) — не ломаем
    pass


@mcp.prompt()
def memory_session_start() -> str:
    """Протокол старта сессии с памятью Maxgraph (что загрузить и в каком порядке)."""
    return ("Session start with Maxgraph memory: 1) call session_start(project=<slug>) — a budgeted "
            "context block (project facts, owner decisions, open bugs, past sessions, recent events); "
            "use read_graph only when you need the full graph or the slug is unknown; "
            "2) open_nodes for entities you will definitely touch; "
            "3) briefly tell the user what you remembered. "
            "RU: старт сессии — session_start(project=<slug>) — бюджетный блок контекста "
            "(read_graph — только если нужен весь граф), затем open_nodes по нужным сущностям.")


@mcp.prompt()
def memory_session_end() -> str:
    """Протокол завершения сессии: зафиксировать итоги в памяти Maxgraph."""
    return ("Session end with Maxgraph memory: call session_close(project=<slug>, "
            "summary=\"<1-2 sentences of what was done>\", done=[<atomic outcomes: decisions made, "
            "files changed, bugs fixed>], links=[<names of touched entities>]) — one call creates or "
            "merges today's session entity and its \"discussed\" relations (links empty → derived "
            "from today's pulse mechanically). Check open_tasks_hint in the response and close "
            "finished tasks with task_done. "
            "RU: конец сессии — один вызов session_close: итоги + связи + подсказка незакрытых задач.")


@mcp.tool()
def create_entities(entities: list[EntityIn], project: str = "") -> dict:
    """Create multiple new entities in the knowledge graph. name = kebab-case id навсегда (не меняется), title = человеческое имя. Сначала search_nodes — не дублируй существующие. entityType: канонические project/session/bug/decision/request/event/task (синонимы RU/EN канонизируются: bugfix→bug, решение→decision, просьба→request, задача→task; свободный тип сохраняется как есть); bug получает status open/fixed, task — open/done (default open). project: kebab-slug проекта — сущность project:<slug> создаётся автоматически, существующее имя в чужом проекте → warning без merge."""
    ro = _readonly_guard()
    if ro:
        return ro
    blocked = _write_block()
    if blocked:
        return blocked
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_create_entities([e.model_dump() for e in entities], slug)


@mcp.tool()
def create_relations(relations: list[RelationIn], project: str = "") -> dict:
    """Create multiple relations. Active voice only: "uses", "depends on", "configured by" — не "is used by". project: kebab-slug проекта (события пульса получат его метку)."""
    ro = _readonly_guard()
    if ro:
        return ro
    blocked = _write_block()
    if blocked:
        return blocked
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_create_relations([{"from": r.from_, "to": r.to, "relationType": r.relationType} for r in relations], slug)


@mcp.tool()
def add_observations(observations: list[ObservationsIn], project: str = "") -> dict:
    """Add new observations to existing entities. Одно наблюдение = один атомарный факт (с датой, если важно). Пиши СРАЗУ, как узнал, — не в конце разговора. Факт УСТАРЕЛ (цена, версия, решение) — не удаляй и не дублируй: передай старую строку в supersedes того же item (пометится [~дата] и уйдёт из поиска/session_start, оставшись в архиве; вернуть — update_entity(restore=[...]); физически удалить можно всегда — delete_observations). Ненайденные строки — в supersedesNotFound ответа. contents: элемент — строка или {"text": "...", "source": {"kind": "commit"|"file"|"url"|"issue", "ref": "..."}} (1.6.0; ref: url → http(s)://…, commit → hex sha 7-40); невалидный source — ошибка с индексом элемента. project: kebab-slug — сущности чужого проекта считаются ненайденными."""
    ro = _readonly_guard()
    if ro:
        return ro
    blocked = _write_block()
    if blocked:
        return blocked
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_add_observations([o.model_dump() for o in observations], slug)


@mcp.tool()
def delete_entities(entityNames: list[str], project: str = "") -> dict:
    """Delete multiple entities and their associated relations from the knowledge graph. project: kebab-slug — удаляются только сущности этого проекта"""
    ro = _readonly_guard()
    if ro:
        return ro
    blocked = _write_block()
    if blocked:
        return blocked
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_delete_entities(entityNames, slug)


@mcp.tool()
def delete_observations(deletions: list[DeletionIn], project: str = "") -> dict:
    """Delete specific observations from entities in the knowledge graph. project: kebab-slug — затрагиваются только сущности этого проекта"""
    ro = _readonly_guard()
    if ro:
        return ro
    blocked = _write_block()
    if blocked:
        return blocked
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_delete_observations([d.model_dump() for d in deletions], slug)


@mcp.tool()
def delete_observations_by_id(items: list[ObsIdDeletionIn], project: str = "") -> dict:
    """Точечный forget по id наблюдения. items: [{entity, ids}] — id бери из выдачи search_nodes/open_nodes с with_ids=true (id стабилен: sha256 текста как хранится, включая [~дата]-маркер). Ответ: results[] с deleted/notFound по каждой сущности; отсутствующая сущность — errors[]. project: kebab-slug — затрагиваются только сущности этого проекта"""
    ro = _readonly_guard()
    if ro:
        return ro
    blocked = _write_block()
    if blocked:
        return blocked
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_delete_observations_by_id([i.model_dump() for i in items], slug)


@mcp.tool()
def delete_relations(relations: list[RelationIn], project: str = "") -> dict:
    """Delete multiple relations from the knowledge graph. project: kebab-slug — удаляются только связи с обоими концами в этом проекте"""
    ro = _readonly_guard()
    if ro:
        return ro
    blocked = _write_block()
    if blocked:
        return blocked
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_delete_relations([{"from": r.from_, "to": r.to, "relationType": r.relationType} for r in relations], slug)


@mcp.tool()
def read_graph(project: str = "", all_projects: bool = False) -> dict:
    """Read the knowledge graph. Вызывай ПЕРВЫМ на старте сессии — это твоя долгосрочная память о проекте и пользователе. project: kebab-slug — только этот проект (+ сущность project:<slug>); all_projects=true — явный кросс-поиск по всем; без параметров — весь граф, как раньше."""
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_read_graph(slug, all_projects)


@mcp.tool()
def search_nodes(query: str, project: str = "", all_projects: bool = False, type: str = "",
                 limit: int = 20, with_ids: bool = False,
                 created_from: str = "", created_to: str = "",
                 updated_from: str = "", updated_to: str = "",
                 include_cold: bool = False) -> dict:
    """Search for nodes by query. Используй вместо read_graph, когда нужна одна тема, и ПЕРЕД create_entities — чтобы не создать дубликат. project: kebab-slug — искать только в этом проекте; all_projects=true — явный кросс-поиск. type: фильтр по типу после нормализации (канонические: project/session/bug/decision/request/event/task; «bugfix»/«баг» → bug; свободный тип — точное совпадение). Непустой query — ранжированный FTS-lite: сортировка по релевантности, limit — топ-N (default 20), у сущностей ответа + score, snippet, highlights (позиции совпадений в snippet, без HTML); часто и недавно читаемые сущности дополнительно всплывают (ACT-R). Пустой query — все сущности с фильтрами, как раньше. Ответ включает канонический type, а для bug/task — status (open/fixed и open/done). with_ids=true: observations — объекты {id, text, source?} для delete_observations_by_id; default false — старый формат строк. Даты (1.6.0): created_from/created_to/updated_from/updated_to (YYYY-MM-DD, включительно) — фильтр сущности по датам до скоринга; без них временны́е фразы в query («на прошлой неделе», «last 3 days») распознаются сами (поле по умолчанию updatedAt; «создан/добавлен/created/added» → createdAt; сработавший парсер → meta.temporal в ответе). include_cold (1.9.0, default false): cold-сущности (без обращений 90+ дней, env COLD_AFTER_DAYS) скрыты из выдачи; true — вернуть их (обращение делает их снова тёплыми). Точное имя cold-сущности всегда находится через open_nodes."""
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_search(query, slug, all_projects, type, limit, with_ids=with_ids,
                     created_from=created_from, created_to=created_to,
                     updated_from=updated_from, updated_to=updated_to,
                     include_cold=include_cold)


@mcp.tool()
def open_nodes(names: list[str], project: str = "", with_ids: bool = False) -> dict:
    """Open specific nodes in the knowledge graph by their names. project: kebab-slug — видимы только сущности этого проекта. with_ids=true: observations — объекты {id, text} для delete_observations_by_id; default false — старый формат строк"""
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_open(names, slug, with_ids=with_ids)


@mcp.tool()
def list_timeline(limit: int = 50, project: str = "", all_projects: bool = False) -> dict:
    """List recent memory events (audit log): what was created/updated and when. project: kebab-slug — только события этого проекта; all_projects=true — явно все"""
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_timeline(limit, slug, all_projects)


@mcp.tool()
def session_start(project: str = "", budget_chars: int = 10000) -> dict:
    """ВЫЗЫВАЙ ПЕРВЫМ на старте сессии: бюджетный блок контекста проекта — {context_block, meta}. context_block готов к вставке в промпт: # Проект → ## Решения владельца (decision, дословно) → ## Открытые баги (bug, не закрытые) → ## Открытые задачи (task, status≠done — дела на потом) → ## Глобальная память (decision-сущности спецпроекта global — общие для всех проектов знания; 1.7.0) → ## Прошлые сессии (session, имя + 2 факта) → ## Недавние события. Внутри решений/багов/задач порядок — актуальность: свежесть × частота обращений (ACT-R). Жёсткий бюджет budget_chars (default 10000): блок никогда его не превышает, усечение построчное с маркером «… (ещё N, усечено бюджетом)», флаг meta.truncated. Cold (1.9.0): сущности без обращений 90+ дней (env COLD_AFTER_DAYS, 0=выкл) скрыты из всех секций, meta.cold.skipped — сколько спрятано; точное имя всегда открывается через open_nodes. project ОБЯЗАТЕЛЕН (kebab-slug) — без него весь граф намеренно не читается."""
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    if not slug:
        return {"error": "session_start требует project=<kebab-slug> (напр. \"kimi\"): "
                         "без проекта весь граф намеренно не читается. Slug даёт хост "
                         "(git remote → git root → cwd); список проектов — search_nodes "
                         "по \"project:\" или read_graph."}
    return op_session_start(slug, budget_chars)


@mcp.tool()
def update_entity(name: str, title: str = "", entityType: str = "", tags: list[str] | None = None,
                  status: str = "", restore: list[str] | None = None, project: str = "") -> dict:
    """Update title, type, tags or status of an existing entity. entityType канонизируется (синонимы RU/EN → project/session/bug/decision/request/event/task; свободный тип — как есть). status — для bug (open/fixed) и task (open/done); для остальных типов — игнор с warning в ответе. restore: снять маркер [~дата] с наблюдений (откат supersedes) — строки с маркером или без; ненайденные — в restoreNotFound. project: kebab-slug — сущности чужого проекта считаются ненайденными"""
    ro = _readonly_guard()
    if ro:
        return ro
    blocked = _write_block()
    if blocked:
        return blocked
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_update_entity({"name": name, "title": title, "entityType": entityType,
                             "tags": tags, "status": status, "restore": restore or []}, slug)


# --- (work_state): открытые задачи проекта — task_add/task_done/task_list ---

@mcp.tool()
def task_add(title: str, note: str = "", links: list[str] | None = None, project: str = "") -> dict:
    """Добавить открытое дело «на потом» (work_state): создаёт task-сущность task-<slug(title)> со status=open (коллизия имени → суффикс -2). note — первый факт (опционально: контекст, ссылка). links — имена связанных сущностей (баг, решение, сессия): связи «relates to» создаются в том же вызове, неизвестные имена → links_skipped. Пока задача открыта, она попадает в секцию «Открытые задачи» session_start. Закрытие — task_done (само свяжет задачу к сессии дня). project: kebab-slug проекта."""
    ro = _readonly_guard()
    if ro:
        return ro
    blocked = _write_block()
    if blocked:
        return blocked
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_task_add(title, note, slug, links=links or [])


@mcp.tool()
def task_done(name: str, project: str = "") -> dict:
    """Закрыть задачу: status=done + событие task_done в пульс. Только entityType=task; уже закрытая — идемпотентно (already: true). project: kebab-slug — задачи чужого проекта считаются ненайденными."""
    ro = _readonly_guard()
    if ro:
        return ro
    blocked = _write_block()
    if blocked:
        return blocked
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_task_done(name, slug)


@mcp.tool()
def task_list(project: str = "") -> dict:
    """Открытые задачи (status≠done), recency-first — что сейчас висит в работе. project: kebab-slug — только этот проект; без project — все открытые задачи графа. Операция чтения (пульс не пишется)."""
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_task_list(slug)


@mcp.tool()
def promote_to_global(name: str, project: str = "") -> dict:
    """Перенести сущность в спецпроект global: её decision-факты видны в session_start ЛЮБОГО проекта (секция «Глобальная память»). Запись в global — только явная (этот инструмент или project="global" у create_entities/add_observations). Перенос меняет только поле project (+updatedAt, событие пульса): наблюдения/теги/тип/связи сохраняются. Коллизия имени в global → merge наблюдений с дедупом по тексту (ответ {"merged": true}). project: kebab-slug текущего проекта сущности — сущность вне него считается ненайденной; без project — любой проект, кроме global."""
    ro = _readonly_guard()
    if ro:
        return ro
    blocked = _write_block()
    if blocked:
        return blocked
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_promote_global(name, slug)


@mcp.tool()
def demote_from_global(name: str, project: str = "") -> dict:
    """Вернуть сущность из спецпроекта global в указанный проект (project ОБЯЗАТЕЛЕН, kebab-slug назначения). Симметричен promote_to_global: меняется только поле project (+updatedAt, событие пульса demote_global); коллизия имени в проекте назначения → merge ({"merged": true})."""
    ro = _readonly_guard()
    if ro:
        return ro
    blocked = _write_block()
    if blocked:
        return blocked
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    if not slug:
        return {"error": "demote_from_global требует project=<kebab-slug> назначения"}
    return op_demote_global(name, slug)


@mcp.tool()
def session_close(summary: str = "", done: list[str] | None = None,
                  links: list[str] | None = None, project: str = "") -> dict:
    """Закрыть сессию дня ОДНИМ вызовом (вызывай в конце сессии): session-сущность дня создаётся или домердживается (в т.ч. в stub от SessionEnd-хука), summary + done становятся наблюдениями, links получают связи "discussed". links не передан → связи собираются механически из сегодняшнего пульса (без LLM, ничего не пишется за тебя — только по твоему явному вызову). Повторный вызов за день — домердж, а не новая сущность. Ответ: session (имя), merged, already_closed, linked, links_auto, links_skipped (неизвестные имена), open_tasks_hint (открытые задачи, тронутые сегодня — закрой через task_done, если сделаны). project: kebab-slug проекта."""
    ro = _readonly_guard()
    if ro:
        return ro
    blocked = _write_block()
    if blocked:
        return blocked
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_session_close(summary, done or [], links or [], slug)


# --------------------------------------------------------- REST + dashboard -

_PAGES = {"landing.html": DATA_DIR / "landing.html",
          "billing.html": DATA_DIR / "billing.html",
          "privacy.html": DATA_DIR / "privacy.html", "offer.html": DATA_DIR / "offer.html"}
# dashboard.html в _PAGES нет (решение владельца 2026-10-20): дашборд всегда из
# образа/бандла — DATA_DIR/dashboard.html больше не шэдоуит бандлённый.
# admin.html нет в _PAGES: страница версионируется в репо.
# DATA_DIR/admin.html на сервере
# НЕ должен шэдоуить репозиторную — иначе деплой отдаёт старый JS под новый API.


def _page_html(name: str) -> str:
    override = _PAGES.get(name)
    if override is not None and override.exists():
        return override.read_text(encoding="utf-8")
    return (_RESOURCE_BASE / name).read_text(encoding="utf-8")


def _dashboard_html() -> str:
    return _page_html("dashboard.html")


# Security-заголовки в одной точке.
# CSP этап 1: страницы однофайловые (инлайн CSS/JS — без 'unsafe-inline' нельзя),
# но внешние скрипты/шрифты, fetch наружу, чужие формы и iframe-встраивание закрыты.
# Без upgrade-insecure-requests: self-hosted ходит по http://LAN.
_NOCACHE = {
    "Cache-Control": "no-store, must-revalidate",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "clipboard-write=(self)",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    ),
}

# Дашборд: connect-src + магазин (покупка Pro из браузера). PUBLIC_BASE_URL
# задан ниже по модулю — тут только константа магазина по умолчанию.
_NOCACHE_DASH = {**_NOCACHE, "Content-Security-Policy": _NOCACHE["Content-Security-Policy"].replace(
    "connect-src 'self'", "connect-src 'self' https://rezon-ai.ru")}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request):
    return PlainTextResponse("ok")


@mcp.custom_route("/api/graph", methods=["GET"])
async def api_graph(request: Request):
    slug, err = _query_project(request)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse(op_read_graph(slug, _truthy(request.query_params.get("all_projects")),
                                      raw_obs=True),
                        headers=_NOCACHE)


@mcp.custom_route("/api/export", methods=["GET"])
async def api_export(request: Request):
    """Полный бэкап: состояние графа + журнал событий. ?project=<slug> — только этот проект."""
    slug, err = _query_project(request)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    with _lock:
        ents, rels = _load()  # объединение шардов (файл на диске уже не полная картина)
        events_raw = EVENTS_FILE.read_text(encoding="utf-8") if EVENTS_FILE.exists() else ""
    es, rs = (ents, rels) if not slug else _filter_graph(ents, rels, slug, False)
    memory_raw = "".join(json.dumps({"type": "entity", **e}, ensure_ascii=False) + "\n"
                         for e in (es.values() if isinstance(es, dict) else es))
    memory_raw += "".join(json.dumps({"type": "relation", **r}, ensure_ascii=False) + "\n" for r in rs)
    if slug:
        lines = []
        for line in events_raw.splitlines():
            try:
                if json.loads(line).get("project") == slug:
                    lines.append(line)
            except Exception:
                continue
        events_raw = "\n".join(lines) + ("\n" if lines else "")
    return JSONResponse({
        "app": "maxgraph",
        "version": VERSION,
        "exportedAt": now_iso(),
        "memory_ndjson": memory_raw,
        "events_jsonl": events_raw,
    }, headers={**_NOCACHE, "Content-Disposition": f'attachment; filename="maxgraph-export-{now_iso()[:10]}.json"'})


# --- NDJSON-импорт (1.8.0): экспорт /api/export обратно в граф -------------
# Доверенный вход (Bearer мастер-токена): гварды псевдо-проектов
# create_entities тут НЕ действуют — иначе round-trip не восстановил бы
# структурные project:<slug>-маркеры. Валидация ВСЕГО до _save (атомарно).

def _check_import_entity(rec) -> str:
    """Строка NDJSON type=entity → "" | имя невалидного поля (строка в skipped_invalid)."""
    name = rec.get("name")
    if not isinstance(name, str) or not name.strip():
        return "name"
    for k in ("title", "entityType", "createdAt", "updatedAt"):
        if rec.get(k) is not None and not isinstance(rec[k], str):
            return k
    tags = rec.get("tags")
    if tags is not None and (not isinstance(tags, list)
                             or any(not isinstance(t, str) for t in tags)):
        return "tags"
    obs = rec.get("observations")
    if obs is not None:
        if not isinstance(obs, list):
            return "observations"
        for c in obs:
            if _check_obs_content(c):
                return "observations"
    if rec.get("project") is not None:
        _, perr = _check_project(rec["project"])
        if perr:
            return "project"
    if rec.get("status") is not None:
        _, serr = _check_status(rec["status"], _canonical_type(rec.get("entityType")))
        if serr:
            return "status"
    return ""


def op_import_ndjson(ndjson: str, project: str = "", dry_run: bool = True) -> dict:
    """Ядро POST /api/import: NDJSON формата /api/export (строки
    {"type":"entity"|"relation", ...}) → слияние в граф.

    dry_run (default) → только отчёт, ничего не пишет. apply: merge по name —
    существующие доливаются observations (дедуп по _norm_obs, объекты
    {text,source} тоже; createdAt остаётся старым, updatedAt = max при реальной
    доливке), новые создаются с createdAt записи; relations дедуп по
    (from,to,relationType). Параметр project перебивает project записей.
    Невалидные строки → skipped_invalid (1-based номера). Отчёт dry_run
    совпадает с фактическим apply (один код-путь, _save только при apply)."""
    entities_in, relations_in, skipped = [], [], []
    for lineno, raw_line in enumerate(ndjson.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            skipped.append(lineno)
            continue
        if not isinstance(rec, dict):
            skipped.append(lineno)
            continue
        kind = rec.get("type")
        if kind == "entity":
            if _check_import_entity(rec):
                skipped.append(lineno)
                continue
            entities_in.append(rec)
        elif kind == "relation":
            if not all(isinstance(rec.get(k), str) and rec.get(k).strip()
                       for k in ("from", "to", "relationType")):
                skipped.append(lineno)
                continue
            relations_in.append(rec)
        else:
            skipped.append(lineno)
    ts = now_iso()
    report = {"dry_run": dry_run, "entities_new": 0, "entities_merged": 0,
              "observations_added": 0, "relations_new": 0,
              "skipped_invalid": skipped, "errors": []}
    with _lock:
        ents, rels = _load()
        rel_keys = {(r["from"], r["to"], r["relationType"]) for r in rels}
        for rec in entities_in:
            # project записи нормализуем (_check_project уже прошёл валидацию):
            # сырой "KIMI" иначе упал бы в flat-шард — _shard_of_entity lowercase-only
            slug = project or (_check_project(rec["project"])[0]
                               if rec.get("project") else "")
            name = rec["name"].strip()
            obs_in = rec.get("observations") or []
            ex = ents.get(name)
            if ex is not None:
                report["entities_merged"] += 1
                have = {_norm_obs(o) for o in ex["observations"]}
                added = []
                for c in obs_in:
                    k = _norm_obs(c)
                    if k in have:
                        continue
                    have.add(k)
                    added.append(c)
                if added:
                    ex["observations"].extend(added)
                    ex["updatedAt"] = max(ex.get("updatedAt") or "", ts)
                    report["observations_added"] += len(added)
                continue
            have, obs_list = set(), []
            for c in obs_in:
                k = _norm_obs(c)
                if k in have:
                    continue
                have.add(k)
                obs_list.append(c)
            etype = _canonical_type(rec.get("entityType"))
            d = {"name": name,
                 "title": (rec.get("title") or "").strip() or name,
                 "entityType": etype,
                 "tags": list(rec.get("tags") or []),
                 "observations": obs_list,
                 "createdAt": (rec.get("createdAt") or "").strip() or ts,
                 "updatedAt": (rec.get("updatedAt") or "").strip()
                              or (rec.get("createdAt") or "").strip() or ts}
            status, _ = _check_status(rec.get("status"), etype)
            if status and etype in _STATUS_BY_TYPE:
                d["status"] = status
            if slug and not name.startswith("project:"):
                d["project"] = slug
            ents[name] = d
            report["entities_new"] += 1
        for rec in relations_in:
            key = (rec["from"].strip(), rec["to"].strip(),
                   rec["relationType"].strip())
            if key in rel_keys:
                continue
            rel_keys.add(key)
            rels.append({"from": key[0], "to": key[1], "relationType": key[2],
                         "createdAt": (rec.get("createdAt") or "").strip() or ts})
            report["relations_new"] += 1
        changed = bool(report["entities_new"] or report["observations_added"]
                       or report["relations_new"])
        if not dry_run and changed:
            _save(ents, rels)
    _event("import_entities",
           detail=f"n={report['entities_new'] + report['entities_merged']} "
                  f"dry_run={dry_run}", project=project)
    return report


@mcp.custom_route("/api/import", methods=["POST"])
async def api_import(request: Request):
    """Импорт NDJSON формата /api/export: {"ndjson": str, "project"?, "dry_run"}.
    dry_run=true (default!) — только отчёт {entities_new, entities_merged,
    observations_added, relations_new, skipped_invalid[], errors[]}; apply —
    merge по name с дедупом (см. op_import_ndjson). Тело ≤1MB (как ingest),
    Bearer — middleware TokenAuth. В MCP не добавляется (REST достаточно)."""
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    raw, rerr = await _read_capped(request, INGEST_MAX_BODY)
    if rerr is not None:
        return JSONResponse({"error": f"payload too large (≤{INGEST_MAX_BODY} bytes)"},
                            status_code=413)
    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected JSON object"}, status_code=400)
    ndjson = body.get("ndjson")
    if not isinstance(ndjson, str):
        return JSONResponse({"error": "ndjson must be a string"}, status_code=400)
    slug, err = _check_project(body.get("project"))
    if err:
        return JSONResponse({"error": err}, status_code=400)
    dry_run = body.get("dry_run", True)
    if not isinstance(dry_run, bool):
        return JSONResponse({"error": "dry_run must be a boolean"}, status_code=400)
    return JSONResponse(op_import_ndjson(ndjson, slug, dry_run))


@mcp.custom_route("/api/search", methods=["GET"])
async def api_search(request: Request):
    """?q= — непустой запрос → ранжированный FTS-lite (score/snippet/highlights,
    топ-limit, default 20, cap 500); пустой — все сущности с фильтрами, как раньше.
    1.6.0: created_from/created_to/updated_from/updated_to (YYYY-MM-DD) — фильтр
    по датам сущности; без них временны́е фразы в q парсятся (meta.temporal).
    1.9.0: ?include_cold=1 — включить cold-сущности (по умолчанию скрыты)."""
    slug, err = _query_project(request)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    try:
        limit = max(1, min(int(request.query_params.get("limit", "") or 0)
                           or searchrank.DEFAULT_LIMIT, 500))
    except (TypeError, ValueError):
        limit = searchrank.DEFAULT_LIMIT
    import asyncio
    # op_search при включённом embed делает синхронную
    # сеть (embed.query_vector) — в async-хендлере это блокирует весь event loop.
    # Уводим в threadpool, как остальные сетевые вызовы REST-слоя.
    qp = request.query_params
    return JSONResponse(await asyncio.to_thread(
        op_search, qp.get("q", ""), slug,
        _truthy(qp.get("all_projects")),
        qp.get("type", ""), limit,
        created_from=qp.get("created_from", ""),
        created_to=qp.get("created_to", ""),
        updated_from=qp.get("updated_from", ""),
        updated_to=qp.get("updated_to", ""),
        raw_obs=True,  # дашборд рендерит source-чипы; MCP остаётся текстовым
        include_cold=_truthy(qp.get("include_cold"))))  # 1.9.0: ?include_cold=1


# ---------------------------------------------------- recall -----

# Символ атрибуции фактов из памяти: клиент-хук помечает им строки, вставленные
# в контекст агента из /api/recall и /api/session-context. Формат строки —
# на клиенте-хуке (hooks/); сервер отдаёт чистый JSON, маркер не подставляет.
RECALL_MARKER = "▣"

RECALL_MAX_BODY = 65536          # лимит тела POST /api/recall (64KB, как api_ingest)
RECALL_MAX_QUERY = 500           # query режем до 500 символов
RECALL_MAX_FACT = 300            # один факт ≤300 символов
RECALL_MAX_FACTS_CHARS = 1500    # суммарный потолок фактов в ответе
RECALL_TOP_ENTITIES = 3          # сущностей в ответе (из top-10 op_search; решение владельца 2026-10-19: топ-3)
RECALL_MIN_SCORE = float(os.environ.get("MG_RECALL_MIN_SCORE", "0.85"))  # высокий порог релевантности (решение владельца 2026-10-19; эмпирика: шум по ACT-R-популярности до ~0.75, сильные совпадения ≥0.9)
RECALL_MIN_COS = float(os.environ.get("MG_RECALL_MIN_COS", "0.82"))  # хвостовой гейт по косинусу e5 (замер research/recall-eval-2026-10-20: <0.82 — чистый мусор, хорошие блоки от 0.843)
RECALL_RATE_LIMIT = 120          # запросов/мин с IP (общий in-memory лимитер)
SESSION_CONTEXT_DEFAULT_BUDGET = 4000   # budget_chars /api/session-context по умолчанию


@mcp.custom_route("/api/session-context", methods=["GET"])
async def api_session_context(request: Request):
    """Фаза 1 «MaxGraph recall»: op_session_start как REST для
    клиент-хуков — обратное направление памяти (память сама приходит в контекст
    агента). Ответ — {"context_block", "meta"} как есть.

    ?project=<slug> обязателен: пустой → 400 {"error": "project is required"},
    невалидный → 400 (_check_project). budget_chars: default 4000, клэмп
    500..20000. Чтение: READONLY не блокирует (как /api/search — чтение
    разрешено всегда, _readonly_guard тут не вызывается)."""
    if not str(request.query_params.get("project") or "").strip():
        return JSONResponse({"error": "project is required"}, status_code=400)
    slug, err = _query_project(request)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    ip = _client_ip(request)
    if _rate_limited(ip, "session_context", RECALL_RATE_LIMIT, 60):
        return JSONResponse({"error": "too_many_requests"}, status_code=429,
                            headers={"Retry-After": "60"})
    budget = max(500, min(_safe_int(request.query_params.get("budget_chars"),
                                   SESSION_CONTEXT_DEFAULT_BUDGET), 20000))
    import asyncio
    # op_session_start читает граф/пульс синхронно (а op_search внутри recall
    # может ходить в сеть за embeddings) — в async-хендлере уводим в threadpool,
    # как api_search.
    return JSONResponse(await asyncio.to_thread(op_session_start, slug, budget),
                        headers=_NOCACHE)


@mcp.custom_route("/api/recall", methods=["POST"])
async def api_recall(request: Request):
    """Фаза 1 «MaxGraph recall»: точечный recall по запросу —
    {"project": slug, "query": str} → лучшие факты лучших сущностей.

    op_search(limit=10) → ≤5 сущностей со score>0 → ≤2 факта на сущность
    (простая эвристика: число токенов запроса в тексте, при равенстве — свежее;
    факт ≤300 chars; инвалидированные [~дата] и пустые исключены); суммарно
    ≤1500 chars фактов — дальше хвост списка обрезается. Гейты: score <
    RECALL_MIN_SCORE (0.85) и, при включённом embed, cos < RECALL_MIN_COS (0.82)
    отсекаются.

    Чтение: без _readonly_guard/_write_block. Rate-limit 120/мин с IP,
    тело ≤64KB → 413, невалидный JSON → 400."""
    ip = _client_ip(request)
    if _rate_limited(ip, "recall", RECALL_RATE_LIMIT, 60):
        return JSONResponse({"error": "too_many_requests"}, status_code=429,
                            headers={"Retry-After": "60"})
    # тело читаем через _read_capped — потоковый
    # кап по факту длины (chunked/врущий Content-Length не раздует память);
    # его 400 маппим в наш 413.
    raw, rerr = await _read_capped(request, RECALL_MAX_BODY)
    if rerr is not None:
        return JSONResponse({"error": f"payload too large (≤{RECALL_MAX_BODY} bytes)"},
                            status_code=413)
    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected JSON object"}, status_code=400)
    slug, err = _check_project(body.get("project"))
    if err:
        return JSONResponse({"error": err}, status_code=400)
    if not slug:
        return JSONResponse({"error": "project is required"}, status_code=400)
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return JSONResponse({"error": "query is required"}, status_code=400)
    query = query.strip()[:RECALL_MAX_QUERY]
    import asyncio
    # op_search при включённом embed делает синхронную сеть (embed.query_vector)
    # — в async-хендлере это блокирует весь event loop; уводим в threadpool.
    return JSONResponse(await asyncio.to_thread(_recall, query, slug),
                        headers=_NOCACHE)


def _recall(query: str, slug: str) -> dict:
    """Ядро /api/recall (синхронное; вызывается из хендлера через to_thread)."""
    res = op_search(query, slug, limit=10)
    qts = set(searchrank.tokenize(query))  # стемы запроса (lowercase)
    # Хвостовой косинусный гейт: embed включён → отсекаем сущности с cos <
    # RECALL_MIN_COS (замер 2026-10-20: score-гейт 0.85 пропускает шум до 1.25,
    # т.к. blended нормализован; косинус ловит семантически пустой хвост).
    # qv — LRU-попадание (op_search только что эмбеддил тот же запрос), сети
    # нет. Нет вектора у сущности / embed выключен → None → пропускаем
    # (fail-open, как весь векторный слой).
    _w, cos_fn, _vtop = _hybrid_search(query)
    facts, total = [], 0
    for e in res.get("entities", []):
        if len(facts) >= RECALL_TOP_ENTITIES:
            break
        if cos_fn is not None:
            try:
                cos = cos_fn(e["name"])
            except Exception as exc:
                cos = None  # fail-open: косинус не должен ронять recall
                log.debug("recall: cos_fn сбой для %s: %s", e.get("name"), exc)
            if cos is not None and cos < RECALL_MIN_COS:
                continue
        score = float(e.get("score") or 0)
        if score < RECALL_MIN_SCORE:
            continue
        obs = [_obs_text(o).strip() for o in e.get("observations", [])
               if _obs_text(o).strip() and not _is_invalidated(o)]
        if not obs:
            continue
        # лучшие ≤2: больше токенов запроса в тексте → выше; при равенстве —
        # свежее (наблюдения append-only: позже в списке = новее).
        best = sorted(range(len(obs)),
                      key=lambda i: (-sum(1 for t in qts if t in obs[i].lower()), -i))
        text = " | ".join(obs[i][:RECALL_MAX_FACT] for i in best[:2])
        if total + len(text) > RECALL_MAX_FACTS_CHARS:
            break  # потолок 1500: хвост списка сущностей обрезаем
        total += len(text)
        facts.append({"entity": e["name"], "title": e.get("title") or e["name"],
                      "type": e.get("type") or e.get("entityType") or "",
                      "score": score, "text": text})
    return {"facts": facts, "query": query, "project": slug}


@mcp.custom_route("/api/timeline", methods=["GET"])
async def api_timeline(request: Request):
    slug, err = _query_project(request)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    limit = int(request.query_params.get("limit", "100"))
    return JSONResponse(op_timeline(min(limit, 500), slug,
                                    _truthy(request.query_params.get("all_projects"))))


def _embed_detail():
    """Деталь семантики для дашборда — источник (env=свой endpoint; hosted
    relay удалён в 1.9.10) или причина «выкл»."""
    src = reason = None
    if EMBED_ON:
        src = "env"
    elif READONLY:
        reason = "readonly"
    else:
        reason = "no_config"
    return {"on": EMBED_ON, "source": src, "reason": reason}


@mcp.custom_route("/api/stats", methods=["GET"])
async def api_stats(request: Request):
    slug, err = _query_project(request)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    g = op_read_graph(slug, _truthy(request.query_params.get("all_projects")))
    obs = sum(len(e["observations"]) for e in g["entities"])
    lic = _license_status()
    out = {
        "version": VERSION,
        "name": "Maxgraph",
        "entities": len(g["entities"]),
        "relations": len(g["relations"]),
        "observations": obs,
        "uptimeSec": int(time.time() - STARTED),
        "fileBytes": _graph_file_bytes(),  # memory.json + шарды projects/
        "license": {"state": lic.get("state"), "daysLeft": lic.get("days_left")},
        "embed": EMBED_ON,
        "embedDetail": _embed_detail(),
    }
    if slug:
        out["project"] = slug
    return JSONResponse(out)


def _body_project(body) -> tuple[str, str]:
    """project из JSON-тела POST: (slug, "") | ("", ошибка). Граница валидации."""
    return _check_project((body or {}).get("project"))


@mcp.custom_route("/api/entities", methods=["POST"])
async def api_entities(request: Request):
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    body = await request.json()
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse(op_create_entities(body.get("entities", []), slug))


@mcp.custom_route("/api/entity/update", methods=["POST"])
async def api_entity_update(request: Request):
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    raw, rerr = await _read_capped(request, 262144)  # кап по факту длины
    if rerr is not None:
        return JSONResponse({"error": "bad request"}, status_code=400)
    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    rest = body.get("restore")  # без типовой валидации — 400
    if rest is not None and not isinstance(rest, (list, str)):
        return JSONResponse({"error": "restore must be an array of strings"},
                            status_code=400)
    return JSONResponse(op_update_entity(body, slug))


# --- global-слой (1.7.0): REST-аналоги promote_to_global/demote_from_global ---

@mcp.custom_route("/api/entity/promote", methods=["POST"])
async def api_entity_promote(request: Request):
    """Перенос сущности в спецпроект global: {name, project?}."""
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    raw, rerr = await _read_capped(request, 262144)  # кап по факту длины
    if rerr is not None:
        return JSONResponse({"error": "bad request"}, status_code=400)
    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad json"}, status_code=400)
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse(op_promote_global(body.get("name", ""), slug))


@mcp.custom_route("/api/entity/demote", methods=["POST"])
async def api_entity_demote(request: Request):
    """Возврат сущности из global в проект: {name, project} — project обязателен."""
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    raw, rerr = await _read_capped(request, 262144)  # кап по факту длины
    if rerr is not None:
        return JSONResponse({"error": "bad request"}, status_code=400)
    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad json"}, status_code=400)
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    if not slug:
        return JSONResponse({"error": "demote требует project=<kebab-slug> назначения"},
                            status_code=400)
    return JSONResponse(op_demote_global(body.get("name", ""), slug))


# --- (work_state): REST-аналоги task_add/task_done/task_list ---

@mcp.custom_route("/api/task/add", methods=["POST"])
async def api_task_add(request: Request):
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    raw, rerr = await _read_capped(request, 262144)  # кап по факту длины
    if rerr is not None:
        return JSONResponse({"error": "bad request"}, status_code=400)
    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    links = body.get("links")  # список строк или отсутствует
    if links is not None and (not isinstance(links, list)
                              or any(not isinstance(x, str) for x in links)):
        return JSONResponse({"error": "links must be an array of strings"},
                            status_code=400)
    return JSONResponse(op_task_add(body.get("title"), body.get("note", ""), slug,
                                    links=links or []))


@mcp.custom_route("/api/task/done", methods=["POST"])
async def api_task_done(request: Request):
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    body = await request.json()
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse(op_task_done(body.get("name", ""), slug))


@mcp.custom_route("/api/tasks", methods=["GET"])
async def api_tasks(request: Request):
    """Открытые задачи (status≠done), recency-first. ?project=<slug> — только этот."""
    slug, err = _query_project(request)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse(op_task_list(slug), headers=_NOCACHE)


@mcp.custom_route("/api/session/close", methods=["POST"])
async def api_session_close(request: Request):
    """REST-аналог session_close — {summary, done[], links[], project}."""
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    body = await request.json()
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    done, links = body.get("done"), body.get("links")
    if ((done is not None and not isinstance(done, list))
            or (links is not None and not isinstance(links, list))):
        return JSONResponse({"error": "done/links must be arrays of strings"},
                            status_code=400)
    return JSONResponse(op_session_close(body.get("summary", ""),
                                         done or [], links or [], slug))


@mcp.custom_route("/api/observations", methods=["POST"])
async def api_observations(request: Request):
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    raw, rerr = await _read_capped(request, 262144)  # кап по факту длины
    if rerr is not None:
        return JSONResponse({"error": "bad request"}, status_code=400)
    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    items = body.get("observations", [])
    if not isinstance(items, list):
        return JSONResponse({"error": "observations must be an array"}, status_code=400)
    for it in items:  # supersedes — список или строка, иначе 400
        if isinstance(it, dict):
            # алиас observations→contents, как в MCP-схеме (REST парсит тело сырым json)
            if "contents" not in it and isinstance(it.get("observations"), list):
                it["contents"] = it["observations"]
            sup = it.get("supersedes")
            if sup is not None and not isinstance(sup, (list, str)):
                return JSONResponse({"error": "supersedes must be an array of strings"},
                                    status_code=400)
    try:
        return JSONResponse(op_add_observations(items, slug))
    except ValueError as ex:  # невалидный элемент contents/source (1.6.0) — с индексом
        return JSONResponse({"error": str(ex)}, status_code=400)


@mcp.custom_route("/api/relations", methods=["POST"])
async def api_relations(request: Request):
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    body = await request.json()
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse(op_create_relations(body.get("relations", []), slug))


@mcp.custom_route("/api/delete/entities", methods=["POST"])
async def api_del_entities(request: Request):
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    body = await request.json()
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse(op_delete_entities(body.get("entityNames", []), slug))


@mcp.custom_route("/api/project/delete", methods=["POST"])
async def api_project_delete(request: Request):
    """Удаление проекта целиком (v1.4.7, дашборд): {"project": slug}.
    Те же гварды, что у /api/delete/entities (Bearer — middleware)."""
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    body_raw, rerr = await _read_capped(request, 65536)
    if rerr is not None:
        return JSONResponse({"error": "payload too large (≤64KB)"}, status_code=413)
    try:
        body = json.loads(body_raw)
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    slug, err = _check_project((body or {}).get("project"))
    if err:
        return JSONResponse({"error": err}, status_code=400)
    if not slug:
        return JSONResponse({"error": "project is required"}, status_code=400)
    return JSONResponse(op_delete_project(slug))


@mcp.custom_route("/api/delete/observations", methods=["POST"])
async def api_del_observations(request: Request):
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    body = await request.json()
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse(op_delete_observations(body.get("deletions", []), slug))


@mcp.custom_route("/api/delete/observations-by-id", methods=["POST"])
async def api_del_observations_by_id(request: Request):
    """forget по id — {"items": [{"entity", "ids": [...]}], "project"?}.
    Те же гварды, что у /api/delete/observations (Bearer — middleware,
    readonly → 403, write_block → 402)."""
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    body_raw, rerr = await _read_capped(request, 262144)  # кап 256KB, как у соседей
    if rerr is not None:
        return JSONResponse({"error": "payload too large (≤256KB)"}, status_code=413)
    try:
        body = json.loads(body_raw)
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    items = body.get("items", [])
    if not isinstance(items, list) or len(items) > 100:
        return JSONResponse({"error": "items must be an array (≤100)"}, status_code=400)
    if any(not isinstance(i, dict) or len(i.get("ids") or []) > 1000 for i in items):
        return JSONResponse({"error": "each item: object with ids (≤1000)"}, status_code=400)
    return JSONResponse(op_delete_observations_by_id(items, slug))


@mcp.custom_route("/api/delete/relations", methods=["POST"])
async def api_del_relations(request: Request):
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    body = await request.json()
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse(op_delete_relations(body.get("relations", []), slug))


# --- приёмник захват-хуков (hooks/maxgraph_hook.py → /api/ingest) ---
# Хуки хоста (Claude Code/DSH) шлют батчи событий из локального spool'а; тяжёлой
# логики тут нет: событие → дайджест ≤200 символов в пульс (source="hook").
# Идемпотентность:
# content-hash (SHA256 hook+project+ts+data) — последние 1000 в ingest-hashes.json
# (ротация), повторная доставка spool'а → {"dedup": n}, без дублей в пульсе.
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd",
               "Release", "Deploy",  # Release/Deploy (1.6.0): CI/деплой-события в пульс
               "GitCommit")          # GitCommit (1.6.0): post-commit хук репозитория
INGEST_MAX_EVENTS = 100       # батч до 100 событий
INGEST_MAX_BODY = 1_000_000   # лимит тела 1MB
INGEST_RATE_LIMIT = 60        # запросов/мин с IP
INGEST_HASHES_FILE = DATA_DIR / "ingest-hashes.json"
INGEST_HASHES_KEEP = 1000     # ротация: храним последние 1000 content-hash


def _ingest_hash(ev: dict) -> str:
    """SHA256 канонического {hook,project,ts,data} — ТА ЖЕ формула, что в
    hooks/maxgraph_hook.py (sort_keys, ensure_ascii=False): хук дедупит spool
    перед отправкой, сервер — повторные доставки."""
    canon = json.dumps({"hook": ev.get("hook", ""), "project": ev.get("project", ""),
                        "ts": ev.get("ts", ""), "data": ev.get("data", {})},
                       sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _ingest_hashes_load() -> list:
    try:
        if INGEST_HASHES_FILE.exists():
            data = json.loads(INGEST_HASHES_FILE.read_text(encoding="utf-8"))
            return [h for h in data.get("hashes", []) if isinstance(h, str)]
    except Exception as ex:  # битый журнал дедупа не роняет приём
        log.warning("ingest-hashes: сбой чтения: %s", ex)
    return []


def _ingest_hashes_save(hashes: list):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = INGEST_HASHES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"hashes": hashes[-INGEST_HASHES_KEEP:]},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(INGEST_HASHES_FILE)
    except Exception as ex:  # приём важнее журнала: дедуп деградирует до in-batch
        log.warning("ingest-hashes: сбой записи: %s", ex)


def _hook_digest(hook: str, data: dict) -> str:
    """Дайджест hook-события для пульса: одна строка, ≤200 символов.
    data — недоверенный ввод (prompt-injection): жёстко режем и схлопываем
    whitespace."""
    def short(v, n):
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        return re.sub(r"\s+", " ", str(v or "")).strip()[:n]

    def key_arg(inp):
        """Ключевой аргумент инструмента без JSON-обёртки (лента памяти —
        человекочитаемая): file_path/description/command/query/… Строка-JSON
        (хук шлёт _cut) распарсивается обратно, прочее — как есть."""
        if isinstance(inp, str):
            s = inp.strip()
            if s.startswith("{"):
                try:
                    inp = json.loads(s)
                except Exception:
                    return short(s, 100)
            else:
                return short(s, 100)
        if isinstance(inp, dict):
            for k in ("file_path", "description", "command", "pattern", "query",
                      "title", "name", "path", "url"):
                v = inp.get(k)
                if isinstance(v, str) and v.strip():
                    return short(v, 100)
            obs = inp.get("observations")  # maxgraph add_observations
            if isinstance(obs, list) and obs and isinstance(obs[0], dict):
                return short(obs[0].get("entityName"), 100)
            names = inp.get("names")       # maxgraph open_nodes
            if isinstance(names, list) and names:
                return short(", ".join(str(x) for x in names[:3]), 100)
            return ""
        return short(inp, 100)

    def resp_text(r):
        """Ответ инструмента: JSON-объект → ok / «ошибка: …», текст — как есть."""
        obj = r
        if isinstance(r, str) and r.strip().startswith("{"):
            try:
                obj = json.loads(r)
            except Exception:
                obj = None
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, str) and err:
                return short(f"ошибка: {err}", 60)
            return "ok"
        return short(r, 60)

    if hook == "PostToolUse":
        tool = short(data.get("tool"), 40)
        # mcp__maxgraph__add_observations → maxgraph:add_observations
        if tool.startswith("mcp__"):
            parts = tool.split("__", 2)
            tool = f"{parts[1]}:{parts[2]}" if len(parts) == 3 else tool[5:]
        arg = key_arg(data.get("input"))
        resp = resp_text(data.get("response"))
        text = f"{tool}: {arg} → {resp}" if arg else f"{tool} → {resp}"
    elif hook == "UserPromptSubmit":
        text = short(data.get("prompt"), 190)
    elif hook == "SessionStart":
        text = f"старт сессии ({short(data.get('source'), 60)})"
    elif hook == "SessionEnd":
        text = f"конец сессии ({short(data.get('reason'), 60)})"
    elif hook in ("Release", "Deploy"):
        text = f"{short(data.get('version'), 100)} {short(data.get('status'), 100)}".strip()
    elif hook == "GitCommit":
        sha = short(data.get("sha"), 40)
        text = (f"{short(data.get('subject'), 150)} ({sha[:7]})" if sha
                else short(data.get("subject"), 190)).strip()
    else:  # Stop
        text = "стоп агента"
    return text[:200]


@mcp.custom_route("/api/ingest", methods=["POST"])
async def api_ingest(request: Request):
    """Приёмник захват-хуков. Bearer — middleware TokenAuth (путь не публичный).
    Тело: {"events": [{"hook","project","ts","data"}, …]} — до 100 событий, ≤1MB.
    Release/Deploy (1.6.0): data {version?, url?, status?} (опционально, строки
    ≤300) → пульс hook_Release/hook_Deploy, сущности не создаются.
    SessionEnd: data.open_tasks (список строк) → открытые task-сущности
    проекта (дедуп по title среди открытых). SessionEnd-сессия + open_tasks всего
    батча пишутся ОДНИМ _load/_save.
    Per-IP rate-limit (общий in-memory лимитер, TRUST_PROXY-aware
    через _client_ip): 429 + Retry-After."""
    ip = _client_ip(request)
    if _rate_limited(ip, "ingest", INGEST_RATE_LIMIT, 60):
        return JSONResponse({"error": "too_many_requests"}, status_code=429,
                            headers={"Retry-After": "60"})
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > INGEST_MAX_BODY:
        return JSONResponse({"error": f"payload too large (≤{INGEST_MAX_BODY} bytes)"},
                            status_code=413)
    raw = await request.body()
    if len(raw) > INGEST_MAX_BODY:
        return JSONResponse({"error": f"payload too large (≤{INGEST_MAX_BODY} bytes)"},
                            status_code=413)
    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list):
        return JSONResponse({"error": "expected {\"events\": [...]}"}, status_code=400)
    if len(events) > INGEST_MAX_EVENTS:
        return JSONResponse({"error": f"batch too large (≤{INGEST_MAX_EVENTS} events)"},
                            status_code=400)
    # валидация всего батча ДО записи: либо принимаем целиком, либо 400
    norm = []
    for ev in events:
        if not isinstance(ev, dict):
            return JSONResponse({"error": "event must be an object"}, status_code=400)
        hook = ev.get("hook")
        if hook not in HOOK_EVENTS:
            return JSONResponse(
                {"error": f"unknown hook {hook!r}: one of {', '.join(HOOK_EVENTS)}"},
                status_code=400)
        slug, err = _check_project(ev.get("project"))
        if err:
            return JSONResponse({"error": err}, status_code=400)
        ts = ev.get("ts", "")
        if not isinstance(ts, str):
            return JSONResponse({"error": "event.ts must be a string"}, status_code=400)
        data = ev.get("data", {})
        if not isinstance(data, dict):
            return JSONResponse({"error": "event.data must be an object"}, status_code=400)
        if hook in ("Release", "Deploy"):  # CI-события: поля опциональны, но строки ≤300
            bad = next((k for k in ("version", "url", "status")
                        if data.get(k) is not None
                        and (not isinstance(data[k], str) or len(data[k]) > 300)), None)
            if bad:
                return JSONResponse(
                    {"error": f"data.{bad} must be a string ≤300 chars"},
                    status_code=400)
        if hook == "GitCommit":  # поля опциональны; капы повторяем — хук уже режет
            bad = next((k for k, cap in (("sha", 100), ("branch", 300), ("subject", 2000))
                        if data.get(k) is not None
                        and (not isinstance(data[k], str) or len(data[k]) > cap)), None)
            if bad:
                return JSONResponse(
                    {"error": f"data.{bad} must be a string within its cap"},
                    status_code=400)
        norm.append({"hook": hook, "project": slug, "ts": ts, "data": data})
    # идемпотентность: content-hash против последних 1000 + внутри батча
    with _lock:
        hashes = _ingest_hashes_load()
        seen = set(hashes)
        fresh = []
        dedup = 0
        for ev in norm:
            h = _ingest_hash(ev)
            if h in seen:
                dedup += 1
                continue
            seen.add(h)
            hashes.append(h)
            fresh.append(ev)
        if fresh:
            _ingest_hashes_save(hashes)
    # лёгкая обработка: дайджест в пульс
    for ev in fresh:
        _event(f"hook_{ev['hook']}", detail=_hook_digest(ev["hook"], ev["data"]),
               project=ev["project"], source="hook")
    # SessionEnd → сессия дня (одна на проект, «если такой сессии сегодня нет»).
    # Полный slug в имени — короткий префикс коллидировал
    # («alpha»/«alps» → «alph»; сессии разных проектов сливались в одно имя).
    # ОДИН _load() на весь батч — check-then-create всех SessionEnd-
    # проектов на одном загруженном графе, ОДИН _save в конце
    # (семантика merge/project-автосоздание/part-of
    # та же — через ядро _create_entities_graph).
    ended = sorted({ev["project"] for ev in fresh if ev["hook"] == "SessionEnd"})
    # Проект появляется в дашборде сразу, с ПЕРВЫМ событием — не ждём первой
    # записи сущности: для каждого slug батча гарантируем структурную
    # сущность project:<slug> (check-then-create в том же ОДНОМ _load/_save).
    seen_slugs = sorted({ev["project"] for ev in fresh if ev["project"]})
    all_slugs = sorted(set(ended) | set(seen_slugs))
    if all_slugs:
        today = now_iso()[:10]
        with _lock:
            ents, rels = _load()
            changed = False
            for slug in all_slugs:
                pname = f"project:{slug}"
                if slug and pname not in ents:
                    # напрямую, не через _create_entities_graph: маркер —
                    # системная сущность (гвард псевдо-проектов security-ревизии
                    # действует на items; событие остаётся плоским, как раньше)
                    ents[pname] = {"name": pname, "title": slug,
                                   "entityType": "project", "tags": [],
                                   "observations": [], "createdAt": now_iso(),
                                   "updatedAt": now_iso()}
                    _event("create_entity", pname, slug, source="hook")
                    changed = True
                if slug not in ended:
                    continue
                name = f"session-{today}-{slug or 'flat'}"
                if name not in ents:
                    n = sum(1 for ev in fresh if ev["project"] == slug)
                    _out, ch = _create_entities_graph(ents, rels, [{
                        "name": name,
                        "title": f"Сессия {today} ({slug or 'flat'})",
                        "entityType": "session",
                        "observations": [f"завершена, {n} событий"],
                    }], project=slug, source="hook")
                    changed = changed or ch
                # (страховка протокола): SessionEnd пришёл, а session_close
                # за день не было (нет тега session-close) → одна строка-маркер в
                # карточку сессии дня: «без итогов». Механика без LLM; дедуп —
                # подстрока маркера (повторный SessionEnd не плодит строки). Если
                # агент закроет позже — маркер остаётся как факт «сначала не
                # закрыл» (тег session-close его не стирает).
                sent = ents.get(name)
                if (sent is not None
                        and sent.get("entityType") == "session"  # F2: не чужая сущность с тем же именем
                        and _SESSION_CLOSE_TAG not in (sent.get("tags") or [])):
                    marker = "завершена без итогов агента (автозапись страховки)"
                    # F1: дедуп по startswith, не по подстроке — пользовательский
                    # текст с такой подстрокой внутри не подавит/не подделает маркер
                    if not any(_obs_text(o).startswith(marker)
                               for o in sent.get("observations", [])):
                        sent.setdefault("observations", []).append(
                            f"{marker}, {now_iso()[:10]}")
                        changed = True
                        _event("session_unclosed_marker",  # F3: маркер виден в аудите
                               detail=f"SessionEnd без session_close: {name}",
                               project=slug, source="hook")
                # (work_state): SessionEnd data.open_tasks → task-сущности
                # (status open). Уже ОТКРЫТАЯ задача проекта с тем же title (без
                # регистра) не дублируется — open_tasks присылают на каждом конце
                # сессии; закрытая (done) с таким title — новая работа, создаётся.
                want = []
                for ev in fresh:
                    if ev["hook"] == "SessionEnd" and ev["project"] == slug:
                        want.extend(_open_tasks_from_data(ev["data"]))
                if want:
                    have = {str(e.get("title") or "").strip().lower()
                            for e in ents.values()
                            if (e.get("project") or "") == slug
                            and e.get("entityType") == "task" and _task_is_open(e)}
                    items, taken = [], set()
                    for title in dict.fromkeys(want):
                        key = title.strip().lower()
                        if key in have:
                            continue
                        have.add(key)
                        tname = _task_name_free(ents, title, taken)
                        taken.add(tname)
                        items.append({"name": tname, "title": title,
                                      "entityType": "task"})
                    if items:
                        _out, ch = _create_entities_graph(ents, rels, items,
                                                          project=slug, source="hook")
                        changed = changed or ch
            if changed:
                _save(ents, rels)
    out = {"ok": True, "accepted": len(fresh), "dedup": dedup}
    return JSONResponse(out)


# ------------------------------------------------------------------ store ----

CODES_FILE = DATA_DIR / "codes.json"
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # без 0/O, 1/I/L
_TIERS = {"pro": {"price": "от 149₽/мес", "label": "Pro"}}  # team убран в один тариф, лесенка длительности
_PAID_STATUSES = ("success", "paid", "completed", "confirmed", "successfully")
_REDEEMABLE_STATUSES = ("new", "sold")
def _plan_for(amount: float, currency: str) -> str:
    """Сумма платежа → план (fallback, когда в записи платежа нет plan): максимальный
    план, чья цена в валюте платежа ≤ суммы. Неизвестная валюта → RUB-пороги
    (Platega/Digiseller у нас рублёвые; крипта конвертится до webhook)."""
    cur = currency.upper() if currency else "RUB"
    best = "pro1"
    for pid in sorted(PLANS, key=lambda p: PLANS[p].get(cur, PLANS[p]["RUB"])):
        if PLANS[pid].get("donation"):
            continue  # донат никогда не конвертируется в лицензию
        if amount >= PLANS[pid].get(cur, PLANS[pid]["RUB"]):
            best = pid
    return best
_codes_lock = threading.Lock()  # read-modify-write целиком — иначе двойные тенанты/платежи
_RECOVERY_COOLDOWN = 3600  # сек; per-code кулдаун recovery (защита от email-бомбинга и key-DoS)


def _load_codes() -> dict:
    with _lock:
        if not CODES_FILE.exists():
            return {"codes": [], "tenants": [], "payments": []}
        try:
            return json.loads(CODES_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            backup = CODES_FILE.with_name(f"codes.corrupt-{int(time.time())}.json")
            try:
                CODES_FILE.replace(backup)
            except OSError:
                pass
            log.error("codes.json повреждён → сохранён как %s (%s); ОТКАЗЫВАЮСЬ работать с пустым", backup, e)
            raise


def _save_codes(data: dict):
    with _lock:
        data["payments"] = data.get("payments", [])[-500:]  # платежи не растут бесконечно
        # атомарная запись tmp+replace (как licensing._save_state): обрыв записи не должен
        # превращать codes.json в битый файл → 500 на всех платёжных путях
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CODES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # ДО replace — без микроокна umask-пермишений
        except OSError:
            pass  # Windows/экзотика — best-effort
        tmp.replace(CODES_FILE)
        try:
            os.chmod(CODES_FILE, 0o600)  # license keys открытым текстом (tenant-токены — только sha256) — только owner
        except OSError:
            pass  # Windows/экзотика — best-effort


def _new_code() -> str:
    import secrets

    def group():
        return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))

    return f"MG-{group()}-{group()}-{group()}"


# --- реестр отозванных ключей + CORS для покупки из дашборда -----------

# CORS только для трёх публичных billing-эндпоинтов (дашборд покупателя ходит на
# магазин кросс-ориджином). Секретов в ответах нет (ref — bearer-секрет покупателя).
_CORS = {"Access-Control-Allow-Origin": "*",
         "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
         "Access-Control-Allow-Headers": "Content-Type",
         "Access-Control-Max-Age": "86400"}


def _key_hash(key: str) -> str:
    """sha256 ключа — в реестре отзывов ключи открытым текстом не лежат."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _is_revoked(data: dict, key: str) -> dict | None:
    """Запись реестра {h, ts, note} для ключа или None. Сравнение — constant-time."""
    h = _key_hash(key)
    for r in data.get("revoked", []):
        if hmac.compare_digest(str(r.get("h", "")), h):
            return r
    return None


_REVOKED_MAX = 500


def _add_revoked(data: dict, key: str, note: str = "") -> None:
    """Добавить ключ в revoke-реестр: дедуп по хешу + потолок (трим старых).
    Без потолка codes.json раздувается и тормозит все биллинговые пути под локом."""
    rev = data.setdefault("revoked", [])
    h = _key_hash(key)
    if not any(hmac.compare_digest(str(r.get("h", "")), h) for r in rev):
        rev.append({"h": h, "ts": time.time(), "note": (note or "")[:254]})
    if len(rev) > _REVOKED_MAX:
        data["revoked"] = rev[-_REVOKED_MAX:]


# --- учёт установок (анонимный install-ping self-hosted копий) ----------

_installs_lock = threading.Lock()
_INSTALL_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# серверный якорь триала по machine fingerprint (sha256 hex)
_FP_RE = re.compile(r"^[0-9a-f]{64}$")
# способ установки в install-ping; неизвестное значение не храним
_INSTALL_FLAVORS = frozenset({"docker", "docker-desktop", "rancher", "python", "native"})
_INSTALLS_CAP = 10000   # потолок installs-дикта; вытеснение самых старых по last_seen
_TRIALS_CAP = 100_000   # потолок fp-реестра; переполнение → пинг обслуживается, новый якорь не создаётся (не 5xx)
_FP_KINDS = frozenset({"composite", "legacy", "container"})
# минимальная валидация email из подписанного payload ключа для записи в
# machines.json (джойн админки «Пользователи»); кривой/пустой — не храним вовсе
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")


def _sanitize_email(value) -> str:
    """email для записи в реестр: lower+strip, кап 254; кривой → '' (не пишем)."""
    em = str(value or "").strip().lower()[:254]
    return em if _EMAIL_RE.match(em) else ""


def _parse_hostfp_body(body: dict) -> tuple:
    """Композитный host-fp из тела запроса → (host_fp, components, fp_kind).

    Битый host_fp — не ошибка запроса, а его отсутствие (дальше работает legacy
    fp, как раньше); components фильтруются по известным тегам и формату
    (hostfp.sanitize_components), fp_kind — по вайтлисту, неизвестное значение
    выводится из наличия host_fp."""
    hf = str(body.get("host_fp") or "").strip().lower()
    if not _FP_RE.match(hf or ""):
        hf = ""
    components = hostfp.sanitize_components(body.get("components")) if hf else {}
    kind = str(body.get("fp_kind") or "").strip().lower()
    if kind not in _FP_KINDS:
        kind = "composite" if hf else ""
    if kind == "composite" and not hf:
        kind = ""  # заявленный composite без валидного host_fp — не composite
    return hf, components, kind


def _adopt_trial_anchor(trials: dict, eff: str, prev_fp: str,
                        components: dict, allow_scoring: bool = True):
    """Усыновить существующий якорь под новым effective fp (зовётся под
    _installs_lock, когда trials[eff] ещё нет) → запись | None.

    Два канала тождества машины:
    1) преемственность установки — installs[iid] до этого пинга сидел на prev_fp
       (та же установка принесла композитный fp вместо legacy);
    2) похомпонентный скоринг против якорей с components (частичная смена
       железа; review-зона помечается флагом review, виден только в админке).
    Перенос сохраняет started_at (якорь не молодеет → переустановка ОС на том же
    железе триал не перезапускает); старый fp помечается alias_of=eff и дальше
    выцветает штатным ghost-GC."""
    def _claim(old_key, verdict):
        t_old = trials[old_key]
        t = dict(t_old)
        t["installs"] = list(t.get("installs") or [])  # iid допишет хендлер (reinstalls-буккипинг)
        t.pop("ghost", None)
        t.pop("ghost_since", None)
        t.pop("volatile", None)   # усыновленный composite — настоящая привязка
        t.pop("last_seen", None)
        if components and not t.get("components"):
            t["components"] = components  # эталон якоря не перезаписываем
        if verdict == "review":
            t["review"] = True
        trials[eff] = t
        t_old["alias_of"] = eff
        return t

    if prev_fp and prev_fp != eff and prev_fp in trials:
        if not trials[prev_fp].get("alias_of"):
            return _claim(prev_fp, "same"), False
    if components and allow_scoring:
        best = None
        # shortcut: линейный скан по якорям с components (их доля мала — композитная
        # эра); при росте trials к капу — индекс по spine-хешу
        for old_key, t_old in trials.items():
            if old_key == eff or not isinstance(t_old, dict) or t_old.get("alias_of"):
                continue
            oc = t_old.get("components")
            if not oc:
                continue
            verdict, pct = hostfp.score(oc, components)
            if verdict == "new":
                continue
            started = t_old.get("started_at") or 0
            if best is None or pct > best[0] or (pct == best[0] and started < best[1]):
                best = (pct, started, old_key, verdict)
        if best:
            return _claim(best[2], best[3]), True
    return None


def _load_installs():
    """None при битом файле — хендлер отвечает fail-open БЕЗ записи (не затираем
    данные transient-ошибкой). Нет файла → свежий каркас."""
    if not INSTALLS_FILE.exists():
        return {"installs": {}}
    try:
        data = json.loads(INSTALLS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("installs", {})
            return data
    except Exception:
        pass
    return None  # битый/не-dict — не ломаем сервер, но и не перезаписываем


def _save_installs(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INSTALLS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(INSTALLS_FILE)


_ghost_gc_day = ""  # UTC-дата последнего ghost-GC (раз в сутки, на install-ping)
_GHOST_TTL_SEC = 90 * 86400  # tombstone-якорь живёт 90д после дрейфа fp, потом стирается
_VOLATILE_TTL_SEC = 7 * 86400  # volatile-якорь (установка без привязки к железу)
                               # без пингов стирается через 7д; машиной не считается


def _gc_ghost_fps(data: dict, now: float) -> tuple:
    """Ghost-tombstone: trials-fp, чьи ВСЕ install_id уже ушли на другой
    fp (installs[iid].fp != fp) — доказуемый дрейф контейнерного fingerprint эпохи
    до HOST_FP: контейнер пересоздавался (автообновление) → fp новый, старый мёртв.
    НЕ удаляем мгновенно: помечаем ghost+ghost_since и стираем после
    _GHOST_TTL_SEC тишины.
    Настоящий «поставил и забыл» (fp == текущему fp его install_id) и недоказуемые
    (install вытеснен капом) не трогаем. Вернувшийся fp воскресает в хендлере.
    Volatile-якоря (fp_kind=container) в ghost не играют: TTL 7д без пингов.
    Зовётся под _installs_lock ДО _save_installs. → (marked [(fp,[live…])], deleted,
    volatile_deleted)."""
    installs = data.get("installs") or {}
    trials = data.get("trials") or {}
    marked, deleted, volatile_deleted = [], [], []
    for fp in list(trials):
        t = trials[fp]
        if t.get("volatile"):
            idle = now - _safe_int(t.get("last_seen") or t.get("started_at"), now)
            if idle > _VOLATILE_TTL_SEC:
                volatile_deleted.append(fp)
                del trials[fp]
            continue
        if t.get("ghost"):
            if now - _safe_int(t.get("ghost_since"), now) > _GHOST_TTL_SEC:
                deleted.append(fp)
                del trials[fp]
            continue
        iids = t.get("installs") or []
        if not iids:
            continue
        lives = set()
        provable = True
        for iid in iids:
            rec = installs.get(iid)
            cur = (rec or {}).get("fp")
            if rec is None or not cur or cur == fp:
                provable = False  # жив или недоказуем — не трогаем
                break
            lives.add(cur)
        if provable:
            t["ghost"] = True
            t["ghost_since"] = _safe_int(t.get("ghost_since"), now) or now
            marked.append((fp, sorted(lives)))
    return marked, deleted, volatile_deleted


def _rebind_ghost_licenses(ghosts) -> int:
    """Привязки ключей к ghost-fp переносит на живой fp той же машины — иначе
    tombstone сиротил бы лицензию. Однозначность: перепривязываем только ghost
    с ровно одним
    живым fp. Зовётся ПОСЛЕ снятия _installs_lock (без вложенности локов;
    _machines_lock берётся тут). Fail-safe: любая битость → пропуск, ping не ломается.
    Возвращает число перепривязанных fp."""
    ghost2live = {g: lives[0] for g, lives in ghosts if len(lives) == 1}
    if not ghost2live:
        return 0
    try:
        with _machines_lock:
            mdata = _load_machines()
            moved = 0
            changed = False
            for rec in (mdata.get("machines") or {}).values():
                if not isinstance(rec, dict):
                    continue
                fps = rec.get("fps") or []
                new = []
                for fp in fps:
                    live = ghost2live.get(fp, fp)
                    if live not in new:
                        new.append(live)
                moved += sum(1 for a, b in zip(fps, new) if a != b)
                if new != fps:  # включая dedup-only (legacy 2-слота) — сохраняем
                    rec["fps"] = new
                    changed = True
                al = rec.get("aliases") or []
                if al:  # алиасы (выцветшие fp) переписываем тем же правилом
                    new_al = []
                    for fp in al:
                        live = ghost2live.get(fp, fp)
                        if live not in new_al:
                            new_al.append(live)
                    if new_al != al:
                        rec["aliases"] = new_al
                        changed = True
            if changed:
                _save_machines(mdata)
            return moved
    except Exception:
        log.exception("ghost-rebind: сбой — лицензии не тронуты, до завтра")
        return 0


# --- host-fp: реестр привязок license key → machine fingerprints ----
# Платный ключ привязан к одной машине; переноса нет — отвязка только
# владельцем магазина через /admin. Ключи храним как sha256 (как
# revoke-реестр), fp — sha256-хеши машин (сырое железо никогда не уходит).

MACHINES_FILE = DATA_DIR / "machines.json"
_machines_lock = threading.Lock()
_MACHINES_MAX_FP = 1      # 1 ключ = 1 машина
_MACHINES_CAP = 10000     # потолок ключей в реестре; вытеснение самых старых по updated


_machines_reset_notified = False  # _event о реинициализации — один раз, не флудим пульс


def _load_machines() -> dict:
    """Нет файла → свежий каркас. Битый файл — fail-closed, как codes.json:
    бэкап + raise (хендлер ответит 500, чужие привязки молча не затираем).
    После corrupt-бэкапа файл отсутствует → реестр стартует ПУСТЫМ —
    не молчим: log.critical + событие в пульс (привязки потеряны до ручного
    восстановления из machines.corrupt-*.json)."""
    if not MACHINES_FILE.exists():
        global _machines_reset_notified
        if not _machines_reset_notified and list(DATA_DIR.glob("machines.corrupt-*.json")):
            _machines_reset_notified = True
            log.critical("machines.json отсутствует, но есть corrupt-бэкап — реестр "
                         "реинициализирован ПУСТЫМ; привязки потеряны до ручного восстановления")
            _event("machines_registry_reset", "", "старт с пустого после corrupt-бэкапа")
        return {"machines": {}}
    try:
        data = json.loads(MACHINES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("machines", {})
            return data
        raise ValueError("machines.json не dict")
    except Exception as e:
        backup = MACHINES_FILE.with_name(f"machines.corrupt-{int(time.time())}.json")
        try:
            MACHINES_FILE.replace(backup)
        except OSError:
            pass
        log.error("machines.json повреждён → сохранён как %s (%s); ОТКАЗЫВАЮСЬ работать с пустым",
                  backup, e)
        raise


def _save_machines(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MACHINES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # ДО replace — без микроокна umask-пермишений
    except OSError:
        pass  # Windows — best-effort
    tmp.replace(MACHINES_FILE)
    try:
        os.chmod(MACHINES_FILE, 0o600)  # привязки — не публичные данные
    except OSError:
        pass  # Windows — best-effort


def _machine_bind(data: dict, key: str, fp: str, now: float,
                  components: dict | None = None, legacy_fp: str = "",
                  email: str = "") -> tuple:
    """Привязать effective fp к ключу (зовётся под _machines_lock) → (verdict, rec|None).

    verdict: known — fp привязан (или его алиас); bound — свободный слот был,
    привязали; rebound — fp заменён по доказательству тождества машины, старый
    ушёл в aliases; review — то же, но скоринг в серой зоне (флаг для админки);
    limit — слоты заняты и тождество не доказано.

    Тождество при занятом слоте доказывается двумя путями: преемственностью
    (та же связка ключ + ранее привязанный legacy fp предъявляет композитный)
    или похомпонентным скорингом против сохранённых components (hostfp.score).
    Legacy-ключ без привязок принимает первый fp, как раньше.

    email — из payload ключа, чисто информативное поле записи для джойна
    админки по владельцу (перевыпуск ключа меняет key_hash, email — нет);
    обновляется при КАЖДОМ validate, включая known — прод самолечится без
    миграций. Кривой/пустой email поле не трогает."""
    h = _key_hash(key)
    rec = data["machines"].get(h)
    em = _sanitize_email(email)
    if rec is not None and em:
        rec["email"] = em  # идемпотентно: значение детерминировано ключом
    fps = (rec or {}).get("fps") or []
    aliases = (rec or {}).get("aliases") or []
    if rec and fp in fps:
        rec["updated"] = now  # свежесть = защита от вытеснения по капу
        if components and not rec.get("components"):
            rec["components"] = components  # дозапись при первом композитном визите
        return "known", rec
    if rec and fp in aliases:
        rec["updated"] = now
        return "known", rec
    if rec is None:
        rec = data["machines"][h] = {"fps": [], "first_seen": now}
        if em:
            rec["email"] = em
    fps = rec.setdefault("fps", [])
    verdict = "bound"
    if len(fps) >= _MACHINES_MAX_FP:
        # continuity (связка ключ + ранее привязанный fp) — только для разовой
        # миграции legacy→composite: после записи components ротация идёт через
        # скоринг, иначе предъявление любого fp из истории = безлимитный перенос
        linked = (legacy_fp and not rec.get("components")
                  and (legacy_fp in fps or legacy_fp in (rec.get("aliases") or [])))
        if linked:
            verdict = "rebound"  # legacy→composite по преемственности install_id+ключа
        elif components and rec.get("components"):
            v, _pct = hostfp.score(rec["components"], components)
            if v == "new":
                return "limit", rec
            verdict = "rebound" if v == "same" else "review"
        else:
            return "limit", rec
        for old in fps:  # старые fp — в алиасы (матчатся как known, слот не едят)
            if old != fp and old not in aliases:
                aliases.append(old)
        del aliases[:-8]  # потолок алиасов: rebound-цикл не раздувает запись
        rec["aliases"] = aliases
        fps[:] = []
    fps.append(fp)
    if components and not rec.get("components"):
        rec["components"] = components  # перезапись при rebound не делаем: реплей
                                        # чужого набора не должен сдвигать эталон
    if verdict == "review":
        rec["review"] = True
    else:
        rec.pop("review", None)  # чистое тождество снимает прошлый флаг
    rec["updated"] = now
    if len(data["machines"]) > _MACHINES_CAP:  # вытеснение самых старых по updated
        stale = sorted(data["machines"], key=lambda k: data["machines"][k].get("updated", 0))
        for k in stale[: len(data["machines"]) - _MACHINES_CAP]:
            del data["machines"][k]
    return verdict, rec


_MACHINE_LIMIT_MSG = ("Ключ уже привязан к другому компьютеру. Отвязка — только через "
                      "владельца магазина: help@rezon-ai.ru")


def _install_id(data_dir=None) -> str:
    """Стабильный анонимный id установки: генерится один раз, живёт в <data>/install.json."""
    f = (data_dir or DATA_DIR) / "install.json"
    try:
        if f.exists():
            iid = json.loads(f.read_text(encoding="utf-8")).get("install_id") or ""
            if iid:
                return iid  # пустой/битый id → перегенерация ниже, не ""
        iid = str(uuid.uuid4())
        f.write_text(json.dumps({"install_id": iid, "created": now_iso()}), encoding="utf-8")
        return iid
    except (OSError, ValueError):
        return str(uuid.uuid4())  # read-only data → эфемерный id, ping всё равно идёт


class _NoRedirectPost(urllib.request.HTTPRedirectHandler):
    """30x → HTTPError: urllib иначе повторяет POST на redirect-target, унося тело
    (license key в license-validate) на чужой хост."""

    def redirect_request(self, *a, **k):
        return None


_NO_REDIRECT_POST_OPENER = urllib.request.build_opener(_NoRedirectPost)


def _post_json(url: str, payload: dict, timeout: int = 5):
    """POST json → распарсенный ответ (dict) или None, если тело не JSON/пустое."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    raw = _NO_REDIRECT_POST_OPENER.open(req, timeout=timeout).read(65536)  # кап, как у _get_json
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _get_json(url: str, timeout: int = 5):
    """GET → распарсенный dict или None (проверка новой версии на магазине).
    Тело ограничено 64 КБ — скомпрометированный ответ не съест память процесса."""
    raw = urllib.request.urlopen(url, timeout=timeout).read(65536)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _client_fp_fields() -> dict:
    """Поля привязки к железу для исходящих запросов на магазин (install-ping,
    license validate, referral/code).

    fp — legacy machine_fingerprint (как раньше). Поверх: host_fp + components
    из hostfp.json (его пишет установщик НА ХОСТЕ — контейнер железо не
    вычисляет и вычислить не может) и fp_kind — класс установки: composite
    (есть hostfp.json), legacy (валидный env HOST_FP) или container (fp из
    контейнерных источников — плывёт при пересоздании). Магазин линкует
    legacy fp как алиас композитного по install_id+ключу."""
    out = {}
    fp = licensing.machine_fingerprint()
    if fp:
        out["fp"] = fp
    hf = hostfp.load_hostfp()
    if hf is not None:
        out["fp_kind"] = "composite"
        out["host_fp"] = hf["host_fp"]
        if hf.get("components"):
            out["components"] = hf["components"]
        return out
    env_fp = (os.environ.get("HOST_FP") or "").strip().lower()
    out["fp_kind"] = "legacy" if env_fp and hostfp.FP64_RE.fullmatch(env_fp) else "container"
    return out


def _telemetry_ping(post=None, env=None, data_dir=None):
    """Анонимный install-ping на магазин: install_id + версия + install_flavor
 + fp (sha256-хеш машины). Всегда ON (выключатель
    TELEMETRY=0 удалён). Магазин отвечает якорем начала триала:
    started_at РАНЬШЕ локального first_seen → отматываем триал на якорь
    (снос data/ не даёт новые 7 дней); позже/битый ответ — игнор. Best-effort:
    оффлайн/таймаут — молча пропускаем, старт сервера не блокируем (fail-open)."""
    env = env if env is not None else os.environ
    if STORE_MODE:
        return
    payload = {"install_id": _install_id(data_dir), "version": VERSION,
               # способ установки (доля python-фолбэков). INSTALL_FLAVOR
               # ставит установщик (install.sh/install.ps1); пусто → autodetect
               # (native exe = PyInstaller frozen).
               "install_flavor": (env.get("INSTALL_FLAVOR") or "").strip()
               or ("native" if _FROZEN else "docker" if os.path.exists("/.dockerenv") else "python")}
    payload.update(_client_fp_fields())
    try:
        resp = (post or _post_json)(PUBLIC_BASE_URL + "/api/telemetry/install", payload, timeout=5)
    except Exception:
        return
    try:  # серверный якорь триала; любая битость ответа — молча fail-open
        ts = (resp or {}).get("trial", {}).get("started_at")
        if ts is not None:
            dd = data_dir or DATA_DIR
            licensing.anchor_trial_start(dd, licensing.get_secret(dd), ts)
    except Exception:
        pass


def _bootstrap_hosted():
    """install-ping (регистрирует install_id+fp на магазине). Hosted embed-relay
    удалён в 1.9.10 (решение владельца 2026-10-25, аудит): контент памяти не
    покидает машину; семантика — только явный EMBED_URL/EMBED_KEY в env
    (осознанный opt-in self-hoster'а)."""
    _telemetry_ping()


def _watchdog_iteration(env):
    """Один прогон watchdog (вынесен из цикла для тестов и decouple).

    телеметрия всегда ON (выключатель TELEMETRY=0 удалён) — параметр
    telemetry упразднён; отправка (ревалидация, апдейт-чип) работает всегда."""
    # апдейт-чип — узнаём свежую версию даже без ключа (триал тоже обновляется)
    try:
        v = _get_json(PUBLIC_BASE_URL + "/api/version", timeout=8)
        if isinstance(v, dict) and v.get("version"):
            licensing.set_latest(DATA_DIR, LICENSE_SECRET, str(v["version"]))
    except Exception:
        pass  # fail-open: апдейт-чип не критичен, лицензию проверяем ниже
    key = licensing.current_key(DATA_DIR, LICENSE_SECRET)
    if key:
        payload = {"key": key}
        payload.update(_client_fp_fields())  # host-fp: магазин учитывает машину ключа
        # + install_id — магазин связывает ключ с инстансом (self-ref блок рефералки)
        payload["install_id"] = _install_id()
        try:
            resp = _post_json(PUBLIC_BASE_URL + "/api/license/validate", payload, timeout=8)
        except Exception as e:
            resp = _http_error_json(e)  # 403 machine_limit — штатный отказ с телом
        if isinstance(resp, dict):
            if resp.get("revoked"):
                if licensing.set_revoked(DATA_DIR, LICENSE_SECRET, resp.get("revoked_at") or time.time()):
                    _event("license_revoked", "", "магазин подтвердил отзыв; grace 72ч")
                    log.warning("license revoked по магазину — grace 72ч до блокировки записи")
            elif resp.get("ok"):
                if licensing.set_revoked(DATA_DIR, LICENSE_SECRET, None):
                    _event("license_unrevoked", "", "магазин снял отзыв")
            elif resp.get("error") == "machine_limit":
                # host-fp: machine_limit ≠ «ключ невалиден» — локальный
                # state НЕ затираем (fail-open), лицензия продолжает работать
                log.warning("watchdog: магазин ответил machine_limit — лицензия сохранена")


def _license_watchdog(env=None):
    """суточная онлайн-ревалидация лицензии на магазине (анти-отзыв).

    Магазин сказал «revoked» → licensing.set_revoked(revoked_at сервера) → grace
    72ч на запись, потом expired; «ok» после unrevoke → отметка снимается.
    Строго fail-open: нет сети/ключа/PUBLIC_BASE_URL, STORE_MODE — спим дальше,
    лицензию не трогаем. Пульс пишем только на переходах.
    телеметрия всегда ON — выключатель TELEMETRY=0 удалён.
    """
    env = env if env is not None else os.environ
    if STORE_MODE:
        return
    time.sleep(300)  # первый прогон через 5 минут после старта
    while True:
        try:
            _watchdog_iteration(env)
        except Exception:
            log.exception("license watchdog: сбой прогона — fail-open, продолжаем")
        time.sleep(86400)


def _code_rec(code: str, tier: str, status: str = "new", note: str = "",
              days: "int | None" = None) -> dict:
    rec = {"code": code, "tier": tier, "status": status, "note": note,
           "createdAt": now_iso(), "soldAt": now_iso() if status == "sold" else None,
           "activatedAt": None, "tenant": None}
    if days is not None:
        rec["days"] = days  # срок лицензии при активации кода; 0 = бессрочно
    return rec


def _license_for_code(data: dict, code: str):
    """licenseKey кода: из записи кода (новые) или из платежа (легаси digiseller/platega,
    где лицензия жила только в payment). → (licenseKey|None, code_rec|None)."""
    rec = next((c for c in data["codes"] if c.get("code") == code), None)
    if rec and rec.get("licenseKey"):
        return rec["licenseKey"], rec
    pay = next((p for p in data.get("payments", [])
                if p.get("code") == code and p.get("licenseKey")), None)
    return (pay or {}).get("licenseKey"), rec


# --- tenant-токены: в codes.json только sha256 ---

def _token_hash(token: str) -> str:
    """sha256 токена: файл хранит хеш, сам токен показывается клиенту один раз."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def migrate_token_hashes() -> int:
    """Одноразовая миграция: легаси-тенанты с plaintext `token` → `token_hash`.

    Вызывается при старте сервера (plaintext известен из файла). Возвращает число
    мигрированных записей.
    """
    n = 0
    with _codes_lock:
        data = _load_codes()
        for t in data["tenants"]:
            if t.get("token") and not t.get("token_hash"):
                t["token_hash"] = _token_hash(t["token"])
                del t["token"]
                n += 1
        if n:
            _save_codes(data)
    if n:
        log.info("tenant tokens → sha256: мигрировано %d", n)
    return n


# XFF доверяем только за своим reverse-proxy (TRUST_PROXY=1 на проде за nginx) —
# X-Forwarded-For доверяем только за своим proxy (TRUST_PROXY=1).
# Дополнительно порт 8787 в docker-compose слушает только 127.0.0.1.
TRUST_PROXY = (os.environ.get("TRUST_PROXY") or "").strip() == "1"


def _client_ip(request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            # ПОСЛЕДНИЙ элемент: его добавляет доверенный proxy
            return xff.split(",")[-1].strip()
    return request.client.host if request.client else "?"


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _read_capped(request: Request, limit: int) -> "tuple[bytes | None, Response | None]":
    """Читать body и капать по ФАКТУ длины — content-length может врать/отсутствовать
    (публичные эндпоинты, memory-DoS через chunked). Fast-path по заголовку
    — дешёвый ранний отказ. Превышение → (None, 400).
    Реальный Request читается ПОТОКОВО с ранним abort (иначе chunked на 1 ГБ
    аллоцируется в память до капа); результат кэшируется в request._body, чтобы
    повторные .json()/.form() работали из кэша. Тестовые стабы — через body()."""
    if _safe_int(request.headers.get("content-length")) > limit:
        return None, JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    if hasattr(request, "stream"):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                return None, JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
            chunks.append(chunk)
        body = b"".join(chunks)
        request._body = body  # кэш для повторных чтений (form-фолбэки и т.п.)
        return body, None
    body = await request.body()
    if len(body) > limit:
        return None, JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    return body, None


def _valid_email(value) -> str | None:
    """Нормализованный email или None. Минимальная валидация: @ без пробелов, ≤254.
    CR/LF/TAB режем сразу: такой адрес вечно ломал бы recovery (EmailMessage → ValueError).
    Запятая/скобки/кавычки: «a@b.c,evil@x» иначе проходило → smtplib
    send_message слал письмо с ключом ОБОИМ адресам (getaddresses по To)."""
    e = str(value or "")[:254].strip().lower()
    if any(c in e for c in "\r\n\t,<>'\""):
        return None
    return e if e and "@" in e and " " not in e else None


def _gen_codes(count: int, tier: str, note: str = "", days: "int | None" = None) -> list:
    with _codes_lock:
        data = _load_codes()
        existing = {c["code"] for c in data["codes"]}
        out = []
        while len(out) < count:
            code = _new_code()
            if code in existing:
                continue
            rec = _code_rec(code, tier, "new", note, days=days)
            data["codes"].append(rec)
            existing.add(code)
            out.append(rec)
        _save_codes(data)
    _event("codes_generated", f"{tier}x{len(out)}", note)
    return out


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request):
    # Self-hosted копия покупателя: домашняя — его дашборд, лендинг ему не нужен
    # (покупка только на сторе). STORE_MODE=1 (магазин): лендинг на /, как раньше.
    if not STORE_MODE:
        return Response(status_code=302, headers={"location": "/app"})
    return await _landing_page(request)


@mcp.custom_route("/landing", methods=["GET"])
async def landing(request: Request):
    # Магазин: лендинг строго на '/', отдельного /landing нет (магазин = лендинг + /admin).
    if STORE_MODE:
        return PlainTextResponse("Not Found", status_code=404)
    try:
        return await _landing_page(request)
    except FileNotFoundError:
        # покупательская сборка без landing.html (витрина без лендинга)
        return PlainTextResponse("Not Found", status_code=404)


async def _landing_page(request: Request):
    html = _page_html("landing.html")
    # Инстанс сообщает лендингу свой режим: на self-hosted (STORE_MODE=0) покупка и
    # облачный триал ведут в стор (PUBLIC_BASE_URL), а не в локальные /billing-create —
    # у покупателя нет ключей продавца. Демка (/app) публично не ссылается нигде.
    flag = {"store": STORE_MODE, "storeUrl": PUBLIC_BASE_URL}
    html = html.replace("</head>", f"<script>window.__MG_STORE__={json.dumps(flag)};</script></head>", 1)
    headers = dict(_NOCACHE)
    if STORE_MODE:
        # LiveInternet-голос в футере лендинга магазина: hit-пиксель с counter.yadro.ru.
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "img-src 'self' data:", "img-src 'self' data: https://counter.yadro.ru")
    else:
        # Self-hosted копия покупателя: маркетинговый счётчик магазина вырезаем по
        # маркерам — внутренний URL инстанса не должен уходить на counter.yadro.ru.
        _li_a = html.find("<!--LiveInternet counter-->")
        _li_b = html.find("<!--/LiveInternet-->")
        if _li_a != -1 and _li_b != -1:
            html = html[:_li_a] + html[_li_b + 20:]
    # атрибуция рефералки — ?ref=XXXX-XXXX → кука mg_ref (30д, Lax) +
    # счётчик переходов. Только известные коды (мусор не засоряет метрики);
    # buybox дальше шлёт код в /billing/create, скидка считается серверно.
    code = _ref_norm(request.query_params.get("ref"))
    if code:
        with _ref_lock:
            refs = _load_referrals()
            rec = refs.get(code) if refs is not None else None
            if rec is not None:
                # first-touch + анти-накрутка: у гостя уже стоит валидная
                # mg_ref → атрибуцию не переписываем и clicks не накручиваем
                existing = _ref_norm(request.cookies.get("mg_ref"))
                if not (existing and refs.get(existing)):
                    rec["clicks"] = _safe_int(rec.get("clicks")) + 1
                    _save_referrals(refs)
                    secure = "; Secure" if request.url.scheme == "https" else ""
                    headers["Set-Cookie"] = (f"mg_ref={code}; Path=/; Max-Age={30 * 86400}; "
                                             f"SameSite=Lax{secure}")
    return HTMLResponse(html, headers=headers)


@mcp.custom_route("/app", methods=["GET"])
async def dashboard_app(request: Request):
    # Дашборд — только у self-hosted инстансов; на магазине такой страницы нет.
    # connect-src + магазин — диалог покупки ходит на rezon-ai.ru
    # (/api/plans, /api/billing/*) прямо из браузера.
    if STORE_MODE:
        return PlainTextResponse("Not Found", status_code=404)
    return HTMLResponse(_page_html("dashboard.html"), headers=_NOCACHE_DASH)


_ADMIN_GATE_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Вход</title>
<style>body{font:14px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif;background:#f6f5f2;color:#17161a;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{background:#fff;border:1px solid #e3e1dc;border-radius:12px;padding:32px;width:min(360px,calc(100vw - 24px))}
h1{font-size:17px;margin:0 0 4px}p{font-size:12px;color:#666;margin:0 0 16px}
input{width:100%;box-sizing:border-box;padding:11px 12px;margin:0 0 12px;border:1px solid #d5d2cb;border-radius:8px;font:inherit}
input:focus-visible{outline:2px solid #17161a;outline-offset:1px}
button{width:100%;padding:12px;border:0;border-radius:8px;background:#17161a;color:#fff;font:inherit;cursor:pointer}
button:hover{background:#2c2a31}
button:focus-visible{outline:2px solid #17161a;outline-offset:2px}
.err{color:#b3261e;font-size:12px;min-height:16px;margin:0 0 8px}</style></head>
<body><form class="card" id="f"><h1>Вход</h1><p id="hint">Для владельца магазина.</p>
<input id="u" autocomplete="username" placeholder="Логин" aria-label="Логин" required>
<input id="p" type="password" autocomplete="current-password" placeholder="Пароль" aria-label="Пароль" required>
<div id="setup" style="display:none">
<input id="p2" type="password" autocomplete="new-password" placeholder="Пароль ещё раз" aria-label="Пароль ещё раз">
<input id="mt" type="password" autocomplete="off" placeholder="Master-токен (один раз)" aria-label="Master-токен (один раз)">
</div>
<div class="err" id="e"></div><button id="btn">Войти</button></form>
<script>
var setup=false;
fetch('/api/admin/me').then(function(r){return r.json()}).then(function(j){setup=!j.configured;
 if(setup){document.getElementById('setup').style.display='';document.getElementById('hint').textContent='Первичная настройка.';document.getElementById('btn').textContent='Настроить';document.querySelector('h1').textContent='Настройка';}}).catch(function(){});
document.getElementById('f').onsubmit=async function(ev){ev.preventDefault();
 var u=document.getElementById('u').value.trim(),p=document.getElementById('p').value,e=document.getElementById('e');
 e.textContent='';
 if(setup){if(p!==document.getElementById('p2').value){e.textContent='пароли не совпадают';return;}
  var mt=document.getElementById('mt').value.trim();if(!mt){e.textContent='нужен master-токен';return;}
  var rs=await fetch('/api/admin/setup',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+mt},body:JSON.stringify({user:u,password:p})});
  if(!rs.ok){e.textContent='ошибка настройки';return;}}
 var r=await fetch('/api/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user:u,password:p})});
 if(!r.ok){e.textContent=r.status===429?'слишком много попыток — подожди 15 минут':'ошибка входа';return;}
 location.reload();};
</script></body></html>"""


@mcp.custom_route("/admin", methods=["GET"])
async def admin_page(request: Request):
    """Кабинет владельца магазина: статистика, ключи, учёт. ТОЛЬКО на инстансе магазина
    (STORE_MODE=1). Страница — из репо (app/admin.html); admin.html
    версионируется, DATA_DIR-оверрайд убран (шэдоуил новую версию на проде).
    Self-hosted инстансы → 404.
    Без валидной сессии отдаётся минимальная gate-страница _ADMIN_GATE_HTML —
    полная админка анонимно не раздаётся."""
    if not STORE_MODE:
        return PlainTextResponse("Not Found", status_code=404)
    if not _admin_auth(request):
        return HTMLResponse(_ADMIN_GATE_HTML, headers=_NOCACHE)
    return HTMLResponse(_page_html("admin.html"), headers=_NOCACHE)


def _store_gate():
    """служебные эндпоинты учёта (покупки/активации/коды/установки, вход
    владельца, платёжный контур) существуют ТОЛЬКО на инстансе магазина. Self-hosted
    копии покупателей отвечают 404 — они не должны ни иметь доступа, ни знать о
    существовании учёта. PlainText — неотличимо от 404 роутера (ответ не выдаёт существование учёта)."""
    return None if STORE_MODE else PlainTextResponse("Not Found", status_code=404)


_OG_FILE = _RESOURCE_BASE / "og.png"  # social-preview; exe: из _MEIPASS (--add-data)


_FONTS_DIR = _RESOURCE_BASE / "static" / "fonts"
_FONT_RE = re.compile(r"[a-z0-9-]+\.woff2")


@mcp.custom_route("/.well-known/security.txt", methods=["GET"])
async def security_txt(request: Request):
    """RFC 9116: точка контакта для security-репортов. Публичная, статичная."""
    return PlainTextResponse("Contact: mailto:help@rezon-ai.ru\n"
                             "Expires: 2027-12-31T23:59:59Z\n"
                             "Preferred-Languages: ru, en\n",
                             media_type="text/plain", headers={"Cache-Control": "public, max-age=86400"})


@mcp.custom_route("/static/fonts/{fname}", methods=["GET"])
async def static_font(request: Request):
    """Self-hosted woff2 (Inter/Caveat) для лендинга — правило «ноль внешних URL».
    Имя файла строго [a-z0-9-].woff2 — path traversal исключён."""
    fname = request.path_params.get("fname", "")
    if not _FONT_RE.fullmatch(fname):
        return PlainTextResponse("Not Found", status_code=404)
    f = _FONTS_DIR / fname
    if not f.exists():
        return PlainTextResponse("Not Found", status_code=404)
    return Response(f.read_bytes(), media_type="font/woff2",
                    headers={"Cache-Control": "public, max-age=604800, immutable"})


@mcp.custom_route("/og.png", methods=["GET"])
async def og_image(request: Request):
    """Open Graph превью для лендинга (og:image). Публичное, кэшируемое."""
    if not _OG_FILE.exists():
        return PlainTextResponse("Not Found", status_code=404)
    return Response(_OG_FILE.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


_FAVICON_FILE = _RESOURCE_BASE / "favicon.ico"  # exe: из _MEIPASS (--add-data)


@mcp.custom_route("/favicon.ico", methods=["GET"])
async def favicon(request: Request):
    """Иконка сайта (логотип «Искра») для dashboard/admin/лендинга.
    Публичная, кэшируемая — как /og.png."""
    if not _FAVICON_FILE.exists():
        return PlainTextResponse("Not Found", status_code=404)
    return Response(_FAVICON_FILE.read_bytes(), media_type="image/x-icon",
                    headers={"Cache-Control": "public, max-age=86400"})


_DEMO_SHOT_LOCAL = _RESOURCE_BASE / "static" / "demo-dashboard-dark.png"
_DEMO_SHOT_DATA = DATA_DIR / "static" / "demo-dashboard-dark.png"  # hot-override


@mcp.custom_route("/static/demo-dashboard-dark.png", methods=["GET"])
async def demo_shot(request: Request):
    """Тёмный скриншот дашборда для демо-блока лендинга. Публичное, кэшируемое."""
    f = _DEMO_SHOT_DATA if _DEMO_SHOT_DATA.exists() else _DEMO_SHOT_LOCAL
    if not f.exists():
        return PlainTextResponse("Not Found", status_code=404)
    return Response(f.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


def _page_or_404(name: str):
    """Страница с диска; отсутствует в этой сборке → 404 (неотличимо от роутера)."""
    override = _PAGES.get(name)
    if override is not None and override.exists():
        return HTMLResponse(override.read_text(encoding="utf-8"), headers=_NOCACHE)
    f = Path(__file__).parent / name
    if not f.exists():
        return PlainTextResponse("Not Found", status_code=404)
    return HTMLResponse(f.read_text(encoding="utf-8"), headers=_NOCACHE)


@mcp.custom_route("/privacy", methods=["GET"])
async def privacy_page(request: Request):
    return _page_or_404("privacy.html")


@mcp.custom_route("/offer", methods=["GET"])
async def offer_page(request: Request):
    return _page_or_404("offer.html")


@mcp.custom_route("/api/codes/generate", methods=["POST"])
async def api_codes_generate(request: Request):
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    body, err = await _read_capped(request, 4096)  # кап по факту длины
    if err is not None:
        return JSONResponse({"error": "bad request"}, status_code=400)
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    tier = body.get("tier", "pro")
    if tier not in _TIERS:
        return JSONResponse({"error": "tier must be pro"}, status_code=400)
    count = _safe_int(body.get("count", 10), 10)
    if not 1 <= count <= 500:
        return JSONResponse({"error": "count must be 1..500"}, status_code=400)
    # срок лицензии при ручной выдаче — days 0..3650, 0 = бессрочно (явно)
    days_raw = body.get("days")
    days = None
    if days_raw is not None:
        try:
            if isinstance(days_raw, bool):
                raise ValueError
            days = int(days_raw)
            if float(days_raw) != days:
                raise ValueError
        except (TypeError, ValueError):
            return JSONResponse({"error": "days must be int 0..3650 (0 = бессрочно)"},
                                status_code=400)
        if not 0 <= days <= 3650:
            return JSONResponse({"error": "days must be int 0..3650 (0 = бессрочно)"},
                                status_code=400)
    return JSONResponse({"created": _gen_codes(count, tier, body.get("note", ""), days=days)},
                        headers=_NOCACHE)


@mcp.custom_route("/api/codes", methods=["GET"])
async def api_codes_list(request: Request):
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    data = _load_codes()
    stats = {}
    for c in data["codes"]:
        key = f'{c["tier"]}:{c["status"]}'
        stats[key] = stats.get(key, 0) + 1
    return JSONResponse({"codes": data["codes"][-200:], "stats": stats,
                         "tenants": len(data["tenants"])}, headers=_NOCACHE)


@mcp.custom_route("/api/telemetry/install", methods=["POST"])
async def api_telemetry_install(request: Request):
    """Анонимный install-ping self-hosted копий (всегда ON, оптаута TELEMETRY=0 нет).
    install_id + версия + install_flavor + опциональный fp (sha256-хеш
    машины) — ни email, ни контента памяти. IP не логируется.
    По fp ведём реестр начала триалов (trials в installs.json) и отвечаем якорем
    started_at: снос data/ у клиента
    триал не сбрасывает. Защита от случайного сброса, не от целенаправленного
    взлома — код открыт."""
    ip = _client_ip(request)
    if _rate_limited(ip, "telemetry", 10, 3600):
        return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
    # лимит тела — кап по ФАКТУ длины (content-length может врать/отсутствовать)
    body, err = await _read_capped(request, 4096)
    if err is not None:
        return JSONResponse({"ok": False, "error": "too_large"}, status_code=413)
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    if not isinstance(body, dict):  # [1,2] / "str" → 400, не 500
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    iid = str(body.get("install_id") or "")
    ver = str(body.get("version") or "")
    if not _INSTALL_ID_RE.match(iid) or not ver or len(ver) > 24:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    fp = str(body.get("fp") or "")
    if fp and not _FP_RE.match(fp):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    host_fp, components, fp_kind = _parse_hostfp_body(body)
    eff = host_fp or fp  # effective id машины: композитный при наличии, иначе legacy
    flavor = str(body.get("install_flavor") or "")[:16]
    now = time.time()
    trial_ts = None
    marked = []
    deleted = []
    vol_deleted = []
    capped = False
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    with _installs_lock:
        data = _load_installs()
        if data is None:  # битый installs.json — fail-open без записи (не затираем)
            return JSONResponse({"ok": True}, headers=_NOCACHE)
        rec = data["installs"].get(iid)
        prev_fp = str((rec or {}).get("fp") or "")  # до перезаписи: линковка legacy→composite
        first = rec is None
        rec = rec or {"first_seen": now, "starts": 0}
        rec.update({"last_seen": now, "version": ver, "starts": int(rec.get("starts") or 0) + 1})
        if flavor in _INSTALL_FLAVORS:  # вайтлист: неизвестное молча не храним (forward-compat)
            rec["install_flavor"] = flavor  # перезаписывается при миграции python → docker
        if eff:
            rec["fp"] = eff  # fp установки — для админки
            if fp_kind:
                rec["fp_kind"] = fp_kind
        data["installs"][iid] = rec
        if len(data["installs"]) > _INSTALLS_CAP:  # вытеснение самых старых по last_seen
            stale = sorted(data["installs"], key=lambda k: data["installs"][k].get("last_seen", 0))
            for k in stale[: len(data["installs"]) - _INSTALLS_CAP]:
                del data["installs"][k]
        if eff:
            trials = data.setdefault("trials", {})
            t = trials.get(eff)
            if t is not None and t.get("ghost"):
                # fp вернулся — воскрес, якорь не тронут
                t.pop("ghost", None)
                t.pop("ghost_since", None)
            if t is None:
                # дневной лимит новых якорей с IP; сверх лимита пинг ок — якорь ни
                # создаётся, ни переезжает до завтра. Преемственность установки
                # (тот же install_id принёс новый fp) лимит не ест — штатная миграция.
                anchor_ip = data.setdefault("anchor_ip", {})
                akey = f"{ip}|{today}"
                budget_ok = _safe_int(anchor_ip.get(akey)) < 3 and len(trials) < _TRIALS_CAP
                adopted = _adopt_trial_anchor(trials, eff, prev_fp, components,
                                              allow_scoring=budget_ok)
                if adopted is not None:
                    t, used_scoring = adopted
                    if used_scoring:
                        anchor_ip[akey] = _safe_int(anchor_ip.get(akey)) + 1
                elif budget_ok:
                    t = trials[eff] = {"started_at": now, "reinstalls": 0, "installs": [iid]}
                    if components:
                        t["components"] = components
                    if fp_kind == "container":
                        # unanchored-установка (нет hostfp.json/HOST_FP): якорь
                        # volatile — в «машины» не считается, без пингов 7д стирается
                        t["volatile"] = True
                        t["last_seen"] = now
                    anchor_ip[akey] = _safe_int(anchor_ip.get(akey)) + 1
                else:
                    capped = True
                if _safe_int(anchor_ip.get(akey)):
                    # вчерашние счётчики не тащим
                    data["anchor_ip"] = {k: v for k, v in anchor_ip.items()
                                         if k.endswith("|" + today)}
            if t is not None:
                seen = t.setdefault("installs", [])
                if iid not in seen:  # новый install_id для этого якоря = переустановка
                    t["reinstalls"] = int(t.get("reinstalls") or 0) + 1
                    if len(seen) < 100:  # служебный перечень — без роста в бесконечность
                        seen.append(iid)
                if components and not t.get("components"):
                    t["components"] = components  # дозапись при апгрейде клиента
                if t.get("volatile"):
                    t["last_seen"] = now
                trial_ts = t.get("started_at")  # якорь НИКОГДА не двигается вперёд
        # ghost-GC (tombstone) раз в UTC-день, до save — персистится тем же файлом
        global _ghost_gc_day
        if _ghost_gc_day != today:
            _ghost_gc_day = today
            try:
                marked, deleted, vol_deleted = _gc_ghost_fps(data, now)
            except Exception:
                marked, deleted, vol_deleted = [], [], []  # GC не должен ломать ping
                log.exception("ghost-GC: сбой — пропущен до завтра")
        _save_installs(data)
    if marked or deleted:
        _event("trials_ghost_gc", "",
               f"ghost помечено: {len(marked)}, стёрто после {_GHOST_TTL_SEC // 86400}д: {len(deleted)}")
        n = _rebind_ghost_licenses(marked)
        if n:
            _event("license_ghost_rebind", "", f"fp перепривязано к живым: {n}")
    if vol_deleted:
        _event("trials_volatile_gc", "",
               f"volatile-якорей стёрто после {_VOLATILE_TTL_SEC // 86400}д тишины: {len(vol_deleted)}")
    if capped:
        _event("trial_anchor_capped", ip, "свыше 3 новых fp за сутки — якорь не создан")
    if first and eff:
        # событие только для fp-анкоренных установок — анонимный флуд
        # uuid'ами не засоряет таймлайн админки (fp флудеру вычислять лень)
        _event("install_seen", iid, ver)
    out = {"ok": True}
    if trial_ts is not None:
        out["trial"] = {"started_at": trial_ts}
        out["trial_days"] = licensing.trial_days()
    return JSONResponse(out, headers=_NOCACHE)


# --- heartbeat живости клиентов ---
_hb_lock = threading.Lock()
_hb_prune_day = ""          # UTC-дата последнего retention-прогона
_HB_RETENTION_SEC = 90 * 86400
_HB_BODY_CAP = 2048
_HB_BYTES_CAP = 8 * 1024 * 1024  # stat-клапан до append: между
                                 # суточными prune файл не растёт безмерно
_HB_OS = frozenset({"windows", "macos", "linux"})
_HB_ID_RE = re.compile(r"^[0-9A-Za-z-]{1,64}$")  # uuid клиента; послабление к _INSTALL_ID_RE
_HB_VER_RE = re.compile(r"^\d+\.\d+\.\d+([-.+][0-9A-Za-z.-]+)?$")


def _hb_prune(now):
    """Retention 90д, раз в UTC-день при первой записи дня: переписать файл
    без старых/битых строк, атомарно.
    shortcut: отдельного cap строк нет — stat-клапан _HB_BYTES_CAP достаточен
    для потока «1 событие / 6ч / установка»."""
    global _hb_prune_day
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    if day == _hb_prune_day or not HEARTBEATS_FILE.exists():
        _hb_prune_day = day
        return
    cutoff = now - _HB_RETENTION_SEC
    kept = []
    try:
        lines = HEARTBEATS_FILE.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError, ValueError):
        lines = []  # битый/нечитаемый файл → начинаем с чистого
    for line in lines:
        try:
            ts = float(json.loads(line).get("ts") or 0)
        except Exception:
            continue  # битая строка выкидывается при prune
        if ts >= cutoff:
            kept.append(line)
    try:  # атомарно: tmp + replace — крэш/disk-full не затирает файл;
        # tmp с pid — два процесса на одном DATA_DIR не затирают чужой tmp
        tmp = HEARTBEATS_FILE.with_name(f"heartbeats.{os.getpid()}.tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, HEARTBEATS_FILE)
    except OSError:
        pass  # read-only data/ — retention пропустим, запись ниже откажет честно
    _hb_prune_day = day


@mcp.custom_route("/api/telemetry/heartbeat", methods=["POST"])
async def api_telemetry_heartbeat(request: Request):
    """Анонимный heartbeat клиентов (телеметрия всегда ON, без оптаута): {install_id, version, os, flavor, ts}. Без fp/license key/IP —
    только живость и факты установки. ts в файле — СЕРВЕРНЫЙ (часам клиента не
    верим), клиентский храним рядом в cts. Только STORE_MODE
    (self-hosted копии отвечают 404 через _store_gate)."""
    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if _rate_limited(ip, "heartbeat", 10, 3600):  # как у install-ping
        return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
    # лимит тела — кап по ФАКТУ длины (content-length может врать/отсутствовать)
    body, err = await _read_capped(request, _HB_BODY_CAP)
    if err is not None:
        return JSONResponse({"ok": False, "error": "too_large"}, status_code=413)
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    if not isinstance(body, dict):  # [1,2] / "str" → 400, не 500
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    iid, ver = body.get("install_id"), body.get("version")
    osname, flavor, cts = body.get("os"), body.get("flavor"), body.get("ts")
    if (not isinstance(iid, str) or not _HB_ID_RE.fullmatch(iid)
            or not isinstance(ver, str) or len(ver) > 20 or not _HB_VER_RE.fullmatch(ver)
            or osname not in _HB_OS
            or not isinstance(flavor, str) or flavor not in _INSTALL_FLAVORS
            or isinstance(cts, bool) or not isinstance(cts, (int, float))):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    now = time.time()
    rec = {"ts": now, "cts": float(cts), "install_id": iid,
           "version": ver, "os": osname, "flavor": flavor}
    with _hb_lock:
        _hb_prune(now)
        try:  # stat-клапан ДО append — файл не растёт сверх капа
            full = (HEARTBEATS_FILE.exists()
                    and HEARTBEATS_FILE.stat().st_size > _HB_BYTES_CAP)
        except OSError:
            full = False
        if full:
            return JSONResponse({"ok": False, "error": "store_full"}, status_code=503)
        try:
            with HEARTBEATS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            # read-only data/ → честный отказ
            return JSONResponse({"ok": False, "error": "store_unavailable"},
                                status_code=503)
    return JSONResponse({"ok": True}, headers=_NOCACHE)


# --- отчёт о баге из дашборда покупателя (mailto у покупателя мёртв) ---
_sup_lock = threading.Lock()
_SUP_BODY_CAP = 16384                    # text 2000 + update_log_tail 4000 + запас на JSON
_SUP_BYTES_CAP = 2 * 1024 * 1024         # stat-клапан до append (конвенция heartbeats)


@mcp.custom_route("/api/support/report", methods=["POST", "OPTIONS"])
async def api_support_report(request: Request):
    # CORS: дашборд покупателя (localhost) шлёт cross-origin POST на магазин —
    # без preflight-ответа fetch не доходит
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS)
    resp = await _support_report_inner(request)
    resp.headers.update(_CORS)
    return resp


async def _support_report_inner(request: Request):
    """Отчёт о баге из дашборда покупателя: {text (обяз.), version?, os?,
    install_id?, update_log_tail?, server_log_errors?} → append в
    data/support_reports.jsonl с СЕРВЕРНЫМ ts. Публичный (как heartbeat), только
    STORE_MODE. Никаких путей/токенов/ключей не принимаем и не храним — только
    симптом и техконтекст, чтобы агент владельца мог отладить по отчёту."""
    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if _rate_limited(ip, "support", 5, 3600):
        return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
    # лимит тела — кап по ФАКТУ длины (content-length может врать/отсутствовать)
    body, err = await _read_capped(request, _SUP_BODY_CAP)
    if err is not None:
        return JSONResponse({"ok": False, "error": "too_large"}, status_code=413)
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    if not isinstance(body, dict):  # [1,2] / "str" → 400, не 500
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    text = body.get("text")
    if not isinstance(text, str):  # text обязателен и только строкой
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    text = text.strip()[:2000]
    if not text:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    # опциональные строки: не-строка → 400 (мусор), длинное — усечь, пустое — не храним
    opt = {}
    for name, cap in (("version", 20), ("os", 20),
                      ("update_log_tail", 4000), ("server_log_errors", 4000)):
        v = body.get(name)
        if v is None:
            continue
        if not isinstance(v, str):
            return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
        v = v[:cap]
        if v:
            opt[name] = v
    iid = body.get("install_id")
    if iid is not None:
        if not isinstance(iid, str) or not _HB_ID_RE.fullmatch(iid):
            return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
        opt["install_id"] = iid
    rec = {"ts": time.time(), "text": text, **opt}
    with _sup_lock:
        try:  # stat-клапан ДО append — файл не растёт сверх капа
            full = (SUPPORT_REPORTS_FILE.exists()
                    and SUPPORT_REPORTS_FILE.stat().st_size > _SUP_BYTES_CAP)
        except OSError:
            full = False
        if full:
            return JSONResponse({"ok": False, "error": "store_full"}, status_code=503)
        try:
            with SUPPORT_REPORTS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            # read-only data/ → честный отказ (как heartbeats)
            return JSONResponse({"ok": False, "error": "store_unavailable"},
                                status_code=503)
    _notify_support_report(rec)  # v1.0.11: письмо владельцу фоном (best-effort)
    return JSONResponse({"ok": True}, headers=_NOCACHE)


# --- v1.0.11: письмо владельцу о новом баг-репорте (только магазин) ---
def _send_support_email(rec: dict):
    """Письмо владельцу о новом отчёте через существующий SMTP-канал billing.
    Получатель — OWNER_NOTIFY_EMAIL; не задан/нет SMTP_HOST → тихий пропуск
    (log.debug). Сбой SMTP → log.warning: отчёт уже на диске, ответ клиенту ушёл.
    Вызывается из daemon-потока — блокировка SMTP не держит HTTP-ответ."""
    to_addr = (os.environ.get("OWNER_NOTIFY_EMAIL") or "").strip()
    if not to_addr or not os.environ.get("SMTP_HOST") or billing is None:
        log.debug("support email: пропуск — OWNER_NOTIFY_EMAIL/SMTP не настроены")
        return
    # version/os попадают в Subject-заголовок: CR/LF недопустимы (клиентские данные)
    ver = str(rec.get("version") or "?").replace("\r", " ").replace("\n", " ")
    osname = str(rec.get("os") or "?").replace("\r", " ").replace("\n", " ")
    subject = f"MaxGraph: новый баг-репорт (v{ver}, {osname})"
    lines = [str(rec.get("text") or ""), "",
             f"version: {ver}", f"os: {osname}"]
    if rec.get("install_id"):
        lines.append(f"install_id: {rec['install_id']}")
    for name in ("update_log_tail", "server_log_errors"):
        v = rec.get(name)
        if v:
            lines += ["", f"--- {name} ---", str(v)]
    try:
        billing.send_email_smtp(
            to_addr, subject, "\n".join(lines),
            {"host": os.environ.get("SMTP_HOST"), "port": os.environ.get("SMTP_PORT"),
             "user": os.environ.get("SMTP_USER"), "password": os.environ.get("SMTP_PASS"),
             "sender": os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER"),
             "starttls": os.environ.get("SMTP_STARTTLS")})
    except Exception as e:
        # type+code, НЕ str(e): SMTPRecipientsRefused тащит адреса (как в recovery)
        log.warning("support email failed: %s code=%s",
                    type(e).__name__, getattr(e, "smtp_code", None))


def _notify_support_report(rec: dict):
    """Отправка в фоновом daemon-потоке: отчёт уже на диске, сеть не держит ответ.
    Поток не делит состояния (env + rec на входе) — thread-safe по построению."""
    threading.Thread(target=_send_support_email, args=(rec,),
                     daemon=True, name="support-email").start()


# --- v1.0.11: вкладка «Баги» в админке — чтение отчётов ТОЛЬКО под админ-сессией ---
_SUP_LIST_CAP = 200


@mcp.custom_route("/api/admin/support-reports", methods=["GET"])
async def api_admin_support_reports(request: Request):
    """Последние 200 отчётов, новые первыми. Только магазин + админ-сессия/Bearer;
    из интернета отчёты недоступны: /api/support/report остаётся write-only,
    этот путь НЕ попадает в PUBLIC_PATHS (только _ADMIN_COOKIE_PATHS).
    Битая строка (обрыв записи) пропускается — выдачу не роняет."""
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    try:
        lines = SUPPORT_REPORTS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        item = {"ts": None, "text": str(rec.get("text") or "")}
        ts = rec.get("ts")
        if isinstance(ts, (int, float)):
            try:
                item["ts"] = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
            except (OverflowError, ValueError):  # экстремальный ts из битой строки —
                pass                             # выдачу не роняем, item["ts"] остаётся None
        for name in ("version", "os", "install_id",
                     "update_log_tail", "server_log_errors"):
            v = rec.get(name)
            item[name] = v if isinstance(v, str) else ""
        out.append(item)
    return JSONResponse({"reports": out[-_SUP_LIST_CAP:][::-1]}, headers=_NOCACHE)


@mcp.custom_route("/api/support/context", methods=["GET"])
async def api_support_context(request: Request):
    """Техконтекст для отчёта о баге (self-hosted, Bearer — как у всех /api/*):
    версия, ОС, хвост update.log и ERROR/WARNING-строки maxgraph.log. Читаются
    только технические журналы программы — память пользователя сюда не попадает."""
    tail, errs = "", []
    try:  # последние ≤4000 символов журнала обновлений (файла может не быть)
        with UPDATE_LOG_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))  # байт с запасом: UTF-8 кириллица ×2
            tail = f.read().decode("utf-8", "replace")[-4000:]
    except OSError:
        pass
    try:  # последние ≤10 строк с ERROR/WARNING из лога сервера (читаем хвост файла)
        with SERVER_LOG_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 262144))  # ротация 1МБ — хвоста 256КБ хватит на 10 строк
            lines = f.read().decode("utf-8", "replace").splitlines()
        errs = [l for l in lines if " ERROR " in l or " WARNING " in l][-10:]
    except OSError:
        pass
    osname = ("windows" if sys.platform.startswith("win")
              else ("macos" if sys.platform == "darwin" else "linux"))
    return JSONResponse({"version": VERSION, "os": osname,
                         "update_log_tail": tail, "server_log_errors": errs},
                        headers=_NOCACHE)


@mcp.custom_route("/api/installs", methods=["GET"])
async def api_installs_list(request: Request):
    """Машины для админки (заменило плоский список установок):
    группировка installs по fp + trials-реестр (якорь, reinstalls) + джойн
    лицензий (machines.json key_hash → tenants/payments → email/tier/key).
    Установки без fp — отдельная группа unknown_installs (машина неизвестна).
    Битые побочные реестры (machines/codes) не роняют выдачу — джойн best-effort."""
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    data = _load_installs() or {"installs": {}}  # битый файл → пустой список, не 500
    installs = data["installs"]
    # ghost-tombstone'ы (доказанный дрейф fp), выцветшие алиасы (legacy→composite)
    # и volatile-якоря (установки без привязки к железу) в витрину машин не идут —
    # якоря их при этом живут; volatile посчитаны отдельно в kpi.
    all_trials = data.get("trials") or {}
    trials = {k: v for k, v in all_trials.items()
              if not v.get("ghost") and not v.get("alias_of") and not v.get("volatile")}
    volatile_n = sum(1 for v in all_trials.values()
                     if isinstance(v, dict) and v.get("volatile") and not v.get("alias_of"))
    fp2kh = {}
    fp2mrec = {}
    try:  # fp → key_hash из machines.json (лицензионные привязки)
        for kh, rec in (_load_machines().get("machines") or {}).items():
            for fp in rec.get("fps") or []:
                fp2kh[fp] = kh
                fp2mrec[fp] = rec
    except Exception:
        pass  # битый machines.json — машины без лицензионного статуса
    kh2lic = {}
    try:  # key_hash → {email, tier, key, revoked}: tenants (активации), платежи — фолбэк
        codes = _load_codes()
        for p in codes.get("payments", []):
            k = p.get("licenseKey")
            if k:
                kh2lic[_key_hash(k)] = {"email": p.get("email") or "",
                                        "tier": p.get("plan") or "", "key": k,
                                        "revoked": bool(_is_revoked(codes, k))}
        for tn in codes.get("tenants", []):  # перезаписывают платёж: активация точнее
            k = tn.get("licenseKey")
            if k:
                kh2lic[_key_hash(k)] = {"email": tn.get("email") or "",
                                        "tier": tn.get("tier") or "", "key": k,
                                        "revoked": bool(_is_revoked(codes, k))}
    except Exception:
        pass
    by_fp = {}
    unknown = []
    # строка машины — канонический fp: выцветшие алиасы (их якоря помечены
    # alias_of) складываются в строку цели; volatile-якоря (установки без
    # привязки к железу) машину не образуют — уходят в unknown_installs
    alias_of = {k: v["alias_of"] for k, v in all_trials.items()
                if isinstance(v, dict) and v.get("alias_of")}
    volatile_fps = {k for k, v in all_trials.items()
                    if isinstance(v, dict) and v.get("volatile")}

    def _canon_fp(fp):
        seen = set()
        while fp in alias_of and fp not in seen:
            seen.add(fp)
            fp = alias_of[fp]
        return fp

    for iid, rec in installs.items():
        fp = rec.get("fp")
        if fp and fp in volatile_fps:
            unknown.append({"install_id": iid, **rec})
        elif fp:
            by_fp.setdefault(_canon_fp(fp), []).append({"install_id": iid, **rec})
        else:
            unknown.append({"install_id": iid, **rec})
    machines = []
    # лицензия может ещё висеть на выцветшем алиасе (ребаинд machines.json — на
    # ближайшем validate): строку алиаса не показываем, но лицензию подтягиваем
    rev_alias = {}
    for old_fp, new_fp in alias_of.items():
        rev_alias.setdefault(new_fp, []).append(old_fp)
    fp_rows = set(by_fp) | set(trials) | {f for f in fp2kh if f not in alias_of}
    for fp in fp_rows:
        t = trials.get(fp) or {}
        mrec = fp2mrec.get(fp) or {}
        all_rows = by_fp.get(fp, [])
        last = max([r.get("last_seen", 0) for r in all_rows] or [0])
        rows = sorted(all_rows,
                      key=lambda r: r.get("last_seen", 0), reverse=True)[:50]  # кап на машину
        kh = fp2kh.get(fp) or ""
        if not kh:
            for a in rev_alias.get(fp, []):
                kh = fp2kh.get(a) or ""
                if kh:
                    break
        machines.append({"fp": fp,
                         "fp_class": "composite" if (t.get("components") or mrec.get("components"))
                         else "legacy",
                         "review": bool(t.get("review") or mrec.get("review")),
                         "started_at": t.get("started_at"),
                         "reinstalls": _safe_int(t.get("reinstalls")),
                         "installs": rows,
                         "license": kh2lic.get(kh),
                         "last_seen": last})
    machines.sort(key=lambda m: m["last_seen"], reverse=True)
    unknown.sort(key=lambda r: r.get("last_seen", 0), reverse=True)
    # KPI «установок» — уникальные install_id (первая регистрация), НЕ пинги/старты:
    # installs — dict {install_id: rec}, повторный пинг того же install_id лишь
    # обновляет rec (starts+1), нового ключа не создаёт. set() — явный дедуп,
    # чтобы счётчик не зависел от формата хранения (инциденты 18.10/19.10).
    unique_installs = len(set(installs))
    return JSONResponse({"machines": machines[:500],
                         "unknown_installs": unknown[:500],
                         "kpi": {"machines": len(machines), "installs": unique_installs,
                                 "volatile": volatile_n}},
                        headers=_NOCACHE)


@mcp.custom_route("/api/payments", methods=["GET"])
async def api_payments_list(request: Request):
    """Учёт платежей для админки: renewal = тот же email покупал раньше (регистр не важен).
    Поле raw (дампы вебхуков) не отдаём — оно служебное и тяжёлое."""
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    pays = sorted(_load_codes().get("payments", []), key=lambda p: p.get("at") or "")
    seen = set()
    for p in pays:
        em = (p.get("email") or "").lower()
        p["renewal"] = bool(em) and em in seen
        if em:
            seen.add(em)
    out = [{k: v for k, v in p.items() if k != "raw"} for p in reversed(pays)]
    # canceled/failed/expired — не продажи: уходят из payments в failed
    ok = [p for p in out if p.get("status") not in _FAILED_STATUSES]
    failed = [p for p in out if p.get("status") in _FAILED_STATUSES]
    return JSONResponse({"payments": ok[:500], "failed": failed[:500]}, headers=_NOCACHE)


@mcp.custom_route("/api/admin/stats", methods=["GET"])
async def api_admin_stats(request: Request):
    """Сводка для вкладки «Статистика» админки: выручка/продажи по дням
    (30 суток, UTC) и воронка триал→Pro по неделям (12 недель): начавших — по
    createdAt, конверсия — paidAt или выданный licenseKey (ручная выдача кода
    тоже считается Pro). Сумма платежа — сохранённая amount (репрайсинг не
    искажает историю); легаси без amount — текущий прайс плана (та же конвенция,
    что на клиенте админки). Вся агрегация здесь — сырые записи не выгружаются."""
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()

    def _ts(val) -> float:
        """epoch из ISO-строки или числа; мусор → 0 (запись пропускаем)."""
        if isinstance(val, bool):
            return 0.0
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val)
                if dt.tzinfo is None:
                    # naive ISO без смещения — трактуем как UTC, а не локальное время
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except (ValueError, OverflowError):
                return 0.0
        return 0.0

    def _week_start(ts: float):
        d = datetime.fromtimestamp(ts, timezone.utc).date()
        return d - timedelta(days=d.weekday())  # понедельник, UTC

    data = _load_codes()
    now = time.time()
    today = datetime.fromtimestamp(now, timezone.utc).date()

    daily: dict = {}
    for p in data.get("payments", []):
        if p.get("status") in _FAILED_STATUSES:  # незавершённые — не продажи (как /api/payments)
            continue
        ts = _ts(p.get("at"))
        if ts <= 0:
            continue
        amount = p.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
            amount = plans.PRICES.get(p.get("plan"), {}).get("RUB", 0)
        day = datetime.fromtimestamp(ts, timezone.utc).date()
        slot = daily.setdefault(day, [0, 0])
        slot[0] += int(amount)
        slot[1] += 1
    days = []
    for i in range(30):
        d = today - timedelta(days=29 - i)
        rev, sales = daily.get(d, (0, 0))
        days.append({"day": d.isoformat(), "revenue": rev, "sales": sales})

    weekly: dict = {}
    for t in data.get("tenants", []):
        ts = _ts(t.get("createdAt"))
        if ts <= 0:
            continue
        slot = weekly.setdefault(_week_start(ts), [0, 0])
        slot[0] += 1
        if t.get("paidAt") or t.get("licenseKey"):
            slot[1] += 1
    w0 = _week_start(now) - timedelta(weeks=11)
    weeks = []
    for i in range(12):
        w = w0 + timedelta(weeks=i)
        trials, conv = weekly.get(w, (0, 0))
        weeks.append({"week": w.isoformat(), "trials": trials, "converted": conv})
    return JSONResponse({"days": days, "weeks": weeks}, headers=_NOCACHE)


@mcp.custom_route("/api/codes/export", methods=["GET", "POST"])
async def api_codes_export(request: Request):
    """Свежие коды для Digiseller. mark=1 (пометить listed, чтобы не продать дважды) —
    ТОЛЬКО по POST: SameSite=Lax шлёт куку на кросс-сайт GET-навигацию, GET с сайд-эффектом
    был бы CSRF-риск. GET всегда read-only."""
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    tier = request.query_params.get("tier", "pro")
    mark = request.method == "POST" and request.query_params.get("mark") == "1"
    include_listed = request.query_params.get("all") == "1"
    with _codes_lock:
        data = _load_codes()
        pool = [c for c in data["codes"] if c["tier"] == tier and
                (c["status"] == "new" or (include_listed and c["status"] == "listed"))]
        lines = [c["code"] for c in pool]
        if mark:
            for c in pool:
                if c["status"] == "new":
                    c["status"] = "listed"
                    c["note"] = (c.get("note", "") + " listed:" + now_iso()).strip()
            _save_codes(data)
    return PlainTextResponse("\n".join(lines) + ("\n" if lines else ""), headers=_NOCACHE)


# общий in-memory рейт-лимитер (сбрасывается при рестарте — приемлемо; персистентный — в ROADMAP)
_rate_buckets: dict = {}
_RATE_GC_AT = 50000  # GC при переполнении: выкидываем ключи без хитов за 24ч
                     # (окно самого длинного лимита = 86400 — семантика не страдает)


def _rate_limited(ip: str, bucket: str, maxn: int, window: int = 60) -> bool:
    import time as _t
    now = _t.time()
    if len(_rate_buckets) > _RATE_GC_AT:
        stale = [k for k, v in _rate_buckets.items() if not v or now - v[-1] > 86400]
        for k in stale:
            _rate_buckets.pop(k, None)
    key = f"{bucket}:{ip}"
    hits = [t for t in _rate_buckets.get(key, []) if now - t < window]
    if len(hits) >= maxn:
        _rate_buckets[key] = hits
        return True
    hits.append(now)
    _rate_buckets[key] = hits
    return False


def _send_recovery_email(email: str, key: str):
    """Восстановление доступа: новый license key уходит ТОЛЬКО письмом, никогда в HTTP-ответе."""
    billing.send_email_smtp(email, "Maxgraph — восстановление ключа",
                            "Твой новый ключ Maxgraph:\n\n"
                            f"{key}\n\n"
                            "Вставь его в дашборд MaxGraph: статус лицензии вверху → «Активировать».\n"
                            "Старый ключ уже заменён и не работает — сохрани это письмо.\n\n"
                            "Не запрашивал восстановление? Напиши нам: help@rezon-ai.ru",
                            {"host": os.environ.get("SMTP_HOST"), "port": os.environ.get("SMTP_PORT"),
                             "user": os.environ.get("SMTP_USER"), "password": os.environ.get("SMTP_PASS"),
                             "sender": os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER"),
                             "starttls": os.environ.get("SMTP_STARTTLS")},
                            html=billing.recovery_html(key))


def _send_redeem_email(email: str, key: str, code: str):
    """Первая активация кода: ключ + инструкция «что дальше» письмом.
    Сбой НЕ роняет выдачу (ключ уже показан на экране) — ловится вызывающим."""
    billing.send_email_smtp(email, "MaxGraph — твой ключ активации",
                            billing.redeem_text(key, code),
                            {"host": os.environ.get("SMTP_HOST"), "port": os.environ.get("SMTP_PORT"),
                             "user": os.environ.get("SMTP_USER"), "password": os.environ.get("SMTP_PASS"),
                             "sender": os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER"),
                             "starttls": os.environ.get("SMTP_STARTTLS")},
                            html=billing.redeem_html(key, code))


@mcp.custom_route("/api/codes/redeem", methods=["POST"])
async def api_codes_redeem(request: Request):
    import secrets
    ip = _client_ip(request)
    if _rate_limited(ip, "redeem", 10, 60):
        return JSONResponse({"error": "too_many_attempts"}, status_code=429)
    body, err = await _read_capped(request, 16384)
    if err is not None:
        return err
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if not isinstance(body, dict):  # "[1,2]" и т.п. — не 500, а 400
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    raw_code = body.get("code")
    code = (raw_code if isinstance(raw_code, str) else "")[:32].strip().upper()
    email = _valid_email(body.get("email"))
    recovery = None  # (email, новый licenseKey, tenant id, старый licenseKey|None) — письмо строго ПОСЛЕ снятия лока
    with _codes_lock:
        data = _load_codes()
        rec = next((c for c in data["codes"] if c["code"] == code), None)
        if not rec:
            return JSONResponse({"ok": False, "error": "Код не найден. Проверь написание."}, status_code=404)
        if rec["status"] == "activated":
            tenant = next((t for t in data["tenants"] if t["id"] == rec.get("tenant")), None)
            if not tenant:
                return JSONResponse({"ok": False, "error": "Код уже активирован."}, status_code=409)
            # Код MG-… —
            # НЕ долговременный секрет (email, Digiseller, success-URL, перепродажа).
            # восстановление = ротация КЛЮЧА АКТИВАЦИИ (раньше ротировался токен
            # кабинета — кабинет отменён, токен дашборд не принимал). Повторный redeem ключ
            # НЕ возвращает никогда: при совпадении email новый ключ уходит письмом
            # (нужен SMTP), иначе — честная отсылка в поддержку.
            t_email = (tenant.get("email") or "").lower()
            if email and t_email and email == t_email:
                if os.environ.get("SMTP_HOST"):
                    # Новый ключ наследует срок старого (истёкший остаётся истёкшим —
                    # recovery не воскрешает просроченное). Старый уходит в revoke ПОСЛЕ
                    # отправки письма: сбой SMTP не убивает действующий ключ.
                    # Кулдаун per-code: код по тред-модели не секрет — без него recovery
                    # спамило бы жертву письмами и ревокало её ключ каждые несколько секунд.
                    last_rec = float(tenant.get("lastRecoveryAt") or 0)  # epoch; см. коммит ниже
                    if time.time() - last_rec < _RECOVERY_COOLDOWN:
                        return JSONResponse({"ok": False, "already": True,
                                             "error": "Восстановление — не чаще раза в час. "
                                                      "Письмо уже отправлено: проверь входящие и спам."},
                                            status_code=429, headers=_NOCACHE)
                    old_lic = tenant.get("licenseKey") or _license_for_code(data, code)[0]
                    exp = 0
                    if old_lic:
                        if _is_revoked(data, old_lic):
                            # revoke доезжает и в recovery: отозванный ключ не воскресает
                            # новым с тем же exp, и kill-switch админа переставал работать
                            return JSONResponse({"ok": False, "already": True,
                                                 "error": "Лицензия отозвана. Вопросы — в поддержку: help@rezon-ai.ru"},
                                                status_code=409, headers=_NOCACHE)
                        payload, _e = licensing.verify_key(LICENSE_SECRET, old_lic, now=0)  # now=0: истёкший тоже читаем
                        if not payload:
                            return JSONResponse({"ok": False, "already": True,
                                                 "error": "Лицензия повреждена. Напиши в поддержку: help@rezon-ai.ru"},
                                                status_code=409, headers=_NOCACHE)
                        exp = int(payload.get("exp") or 0)
                    elif (rec.get("note") or "").strip().lower().startswith("trial"):
                        # облачный триал лицензии не несёт — recovery не превращает триал в Pro
                        return JSONResponse({"ok": False, "already": True,
                                             "error": "Триальный код не несёт лицензии. Pro — после оплаты: rezon-ai.ru/#pricing"},
                                            status_code=409, headers=_NOCACHE)
                    new_lic = licensing.reissue_key(LICENSE_SECRET, t_email,
                                                    tenant.get("tier") or rec["tier"], exp)
                    if new_lic == old_lic:
                        # тот же payload (recovery в ту же секунду, что и выпуск): сдвигаем iat,
                        # иначе revoke старого хеша убил бы и новый ключ (хеши совпадут)
                        new_lic = licensing.reissue_key(LICENSE_SECRET, t_email,
                                                        tenant.get("tier") or rec["tier"], exp,
                                                        now=time.time() + 1)
                    recovery = (t_email, new_lic, tenant["id"], old_lic)
                else:
                    return JSONResponse({"ok": False, "already": True,
                                         "error": "Код уже активирован. Ключ выдаётся один раз; "
                                                  "восстановление — через поддержку: help@rezon-ai.ru"},
                                        status_code=409, headers=_NOCACHE)
            else:
                return JSONResponse({"ok": False, "already": True,
                                     "error": "Код уже активирован. Для восстановления доступа укажи email, "
                                              "на который активирован код."}, status_code=409, headers=_NOCACHE)
        elif rec["status"] not in _REDEEMABLE_STATUSES:
            return JSONResponse({"ok": False, "error": "Код недоступен для активации."}, status_code=409)
        else:
            tenant_id = "t-" + secrets.token_hex(4)
            token = "mg_" + secrets.token_hex(20)
            tenant = {"id": tenant_id, "token_hash": _token_hash(token), "tier": rec["tier"],
                      "createdAt": now_iso(), "code": code, "email": email}
            lic, _ = _license_for_code(data, code)  # платный код — лицензия закреплена за кодом
            if lic and _is_revoked(data, lic):
                # отозванная лицензия (чарджбэк): не отдаём заведомо мёртвый ключ
                return JSONResponse({"ok": False, "error": "Лицензия по этому коду отозвана. "
                                                           "Вопросы — в поддержку: help@rezon-ai.ru"},
                                    status_code=409, headers=_NOCACHE)
            if not lic:
                # ручной код (/api/codes/generate): срок из записи кода — rec.days
                # (0/отсутствует → бессрочная: generate_key days<=0 → exp=0)
                lic = licensing.generate_key(LICENSE_SECRET, email or "buyer@unknown",
                                             rec["tier"], _safe_int(rec.get("days"), 0))
            rec["licenseKey"] = lic  # no-op, если лицензия уже была своя; бэкфилл из платежа
            tenant["licenseKey"] = lic
            tenant["paidAt"] = now_iso()
            data["tenants"].append(tenant)
            rec["status"] = "activated"
            rec["activatedAt"] = now_iso()
            rec["tenant"] = tenant_id
            _save_codes(data)
    if recovery:  # сеть — вне лока; в ответе ключа НЕТ ни при каком раскладе
        import asyncio
        r_email, r_lic, r_tid, r_old = recovery
        try:
            await asyncio.to_thread(_send_recovery_email, r_email, r_lic)
        except Exception as e:
            # type+code, НЕ str(e): SMTPRecipientsRefused тащит dict получателей (PII)
            log.error("recovery email failed for tenant %s: %s code=%s",
                      r_tid, type(e).__name__, getattr(e, "smtp_code", None))
            return JSONResponse({"ok": False, "already": True,
                                 "error": "Письмо не ушло (SMTP). Напиши в поддержку: help@rezon-ai.ru"},
                                status_code=503, headers=_NOCACHE)
        with _codes_lock:  # письмо ушло — закрепляем ротацию (перечитываем: между локами файл мог измениться)
            data = _load_codes()
            t = next((x for x in data["tenants"] if x.get("id") == r_tid), None)
            if t:
                # CAS против гонки двух recovery: если между локами закоммитилась другая
                # ротация, наш отправленный ключ гасим — валидных сирот не остаётся
                cur = t.get("licenseKey") or _license_for_code(data, t.get("code") or "")[0]
                if cur != r_old:
                    _add_revoked(data, r_lic, "recovery race loser")
                    _save_codes(data)
                    return JSONResponse({"ok": False, "already": True,
                                         "error": "Другое восстановление уже выполнено — ключ в почте. "
                                                  "Если письма нет, повтори через час."},
                                        status_code=409, headers=_NOCACHE)
                t["licenseKey"] = r_lic
                t["lastRecoveryAt"] = time.time()  # epoch; кулдаун — только за доставленное письмо
                if r_old:  # старый ключ умирает здесь — письмо честно обещало
                    _add_revoked(data, r_old, f"recovery → {r_email}")
                # код и платёж переключаем на новый ключ: billing/status и success не отдают мёртвый
                rec2 = next((c for c in data["codes"] if c.get("code") == t.get("code")), None)
                if rec2 and rec2.get("licenseKey"):
                    rec2["licenseKey"] = r_lic
                for p in data.get("payments", []):
                    if p.get("code") == t.get("code") and p.get("licenseKey"):
                        p["licenseKey"] = r_lic
                _save_codes(data)
        _event("license_recovery", r_email,
               code if r_old else f"{code} · mint без прежней лицензии")  # аудит ручных кодов
        return JSONResponse({"ok": True, "already": True, "sent": "email",
                             "message": "Новый ключ отправлен на email активации."}, headers=_NOCACHE)
    emailed = False
    if email and billing and os.environ.get("SMTP_HOST"):  # billing=None на self-hosted витрине
        # ключ дублируется письмом с инструкцией. Сбой SMTP НЕ роняет выдачу —
        # активация уже закоммичена, ключ показан на экране (страница скажет про письмо).
        import asyncio
        try:
            await asyncio.to_thread(_send_redeem_email, email, lic, code)
            emailed = True
        except Exception as e:
            # type+code, НЕ str(e): SMTPRecipientsRefused тащит dict получателей (PII)
            log.error("redeem key email failed for tenant %s: %s code=%s",
                      tenant_id, type(e).__name__, getattr(e, "smtp_code", None))
    _event("code_activated", tenant_id, f'{rec["tier"]} · {code}')
    return JSONResponse({"ok": True, "tier": rec["tier"], "token": token, "licenseKey": lic,
                         "emailed": emailed}, headers=_NOCACHE)


# --- реферальная система -------------------------
# Код приглашающего детерминирован из install_id (HMAC), реестр — data/referrals.json
# {code: {install_id, fp, created, clicks, buys, stack_n, history[]}}. Свой лок;
# порядок локов везде одинаковый: _codes_lock/_installs_lock → _ref_lock.

REFERRALS_FILE = DATA_DIR / "referrals.json"
_ref_lock = threading.Lock()
# Crockford-подобный алфавит без визуально неоднозначных 0/O/1/I (+L/U по Crockford)
_REF_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_REF_CODE_RE = re.compile(r"^[2-9A-HJKM-NP-TV-Z]{4}-[2-9A-HJKM-NP-TV-Z]{4}$")
_REF_STRIP_RE = re.compile(r"[^2-9A-HJKM-NP-TV-Za-hjkm-np-tv-z]")


def _ref_code_for(install_id: str) -> str:
    """base32crockford-подобный код: HMAC(REF_HMAC_SECRET, install_id) → 8 символов
    (формат XXXX-XXXX). Детерминирован: код существует, пока существует инстанс."""
    d = hmac.new(REF_HMAC_SECRET.encode(), install_id.encode(), hashlib.sha256).digest()
    n = int.from_bytes(d[:8], "big")
    out = []
    for _ in range(8):
        n, r = divmod(n, len(_REF_ALPHABET))
        out.append(_REF_ALPHABET[r])
    raw = "".join(out)
    return f"{raw[:4]}-{raw[4:]}"


def _ref_norm(raw) -> str:
    """Промокод из запроса → канон XXXX-XXXX (или ""). Регистр/дефис неважны."""
    s = _REF_STRIP_RE.sub("", str(raw or "")).upper()
    if len(s) != 8:
        return ""
    code = f"{s[:4]}-{s[4:]}"
    return code if _REF_CODE_RE.match(code) else ""


def _load_referrals():
    """{} — нет файла; None — битый/не-dict (не затираем transient-ошибкой, как installs)."""
    if not REFERRALS_FILE.exists():
        return {}
    try:
        data = json.loads(REFERRALS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _save_referrals(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REFERRALS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(REFERRALS_FILE)
    try:
        os.chmod(REFERRALS_FILE, 0o600)  # install_id/fp приглашающих — только owner
    except OSError:
        pass  # Windows — best-effort


def _ref_lookup(raw) -> str:
    """Промокод → известный код реестра или "" (невалидный → полная цена, fail-open)."""
    code = _ref_norm(raw)
    if not code:
        return ""
    with _ref_lock:
        refs = _load_referrals()
        if refs is None:
            log.error("referrals.json повреждён — ref-скидка пропущена")
            return ""
        return code if code in refs else ""


_REF_PENDING_TTL = 86400  # брошенный платёж со стеком возвращается через сутки
# N5: refunded/chargeback/reversed намеренно НЕ тут — для оплаченных платежей их
# перехватит дедуп по payment.code раньше; живые терминальные фейлы только такие:
_FAILED_STATUSES = ("failed", "cancelled", "canceled", "expired")


def _ref_restore_locked(refs, payment, flag):
    """Возврат стека платежа владельцу кода. Вызывать под _codes_lock + _ref_lock.
    Идемпотентно по payment['stack_restored']. True = referrals.json изменился."""
    if payment.get("stack_restored"):
        return False
    payment["stack_restored"] = True
    rrec = refs.get(str(payment.get("stack_code") or ""))
    spent = max(0, (_safe_int(payment.get("extend_pct")) - 10) // 10)
    if rrec is None or not spent:
        return False
    rrec["stack_n"] = min(_safe_int(rrec.get("stack_n")) + spent, REF_STACK_CAP)
    rrec.setdefault("history", []).append(
        {"ts": now_iso(), "payment_id": payment.get("ref") or payment.get("id"),
         "amount": payment.get("amount"), "flagged": flag})
    rrec["history"] = rrec["history"][-200:]  # кап истории
    return True


def _ref_sweep_pending():
    """H1: возврат стеков по платежам, созданным со стек-скидкой и не оплаченным
    >TTL. Оплата, дошедшая ПОСЛЕ возврата, сжигает стек повторно в вебхуке
    (гард stack_restored). Порядок локов: _codes_lock → _ref_lock (как в вебхуке)."""
    with _codes_lock:
        data = _load_codes()
        pend = [p for p in (data or {}).get("payments", [])
                if p.get("stack_code") and p.get("status") == "created"
                and not p.get("stack_restored")
                and _safe_int(p.get("stack_ts")) < time.time() - _REF_PENDING_TTL]
        if not pend:
            return
        with _ref_lock:
            refs = _load_referrals()
            if refs is None:
                return
            dirty = False
            for p in pend:
                dirty = _ref_restore_locked(refs, p, "stack_restored_timeout") or dirty
            if dirty:
                _save_referrals(refs)
        _save_codes(data)


def _ref_restore_burned(stack_code: str, burned_n: int):
    """N3: платёж так и не создан (сбой Platega до записи) — вернуть сгоревший
    при создании стек. Свип/вебхук тут не помогут: записи платежа нет."""
    if not stack_code or not burned_n:
        return
    with _ref_lock:
        refs = _load_referrals()
        if refs is None:
            return
        rrec = refs.get(stack_code)
        if rrec is None:
            return
        rrec["stack_n"] = min(_safe_int(rrec.get("stack_n")) + burned_n, REF_STACK_CAP)
        rrec.setdefault("history", []).append(
            {"ts": now_iso(), "flagged": "stack_restore_create_failed"})
        rrec["history"] = rrec["history"][-200:]
        _save_referrals(refs)


@mcp.custom_route("/api/referral/code", methods=["POST"])
async def api_referral_code(request: Request):
    """Магазин: реф-код инстанса → {code, link, clicks, buys, stack_n}.

    Публичный, rate-limit. install_id обязан быть известен по install-ping;
    fp сверяется, если установка его присылала. Код детерминирован —
    запись в referrals.json создаётся лениво при первом запросе (lazy-register).
    install_id+fp — ТОЛЬКО в теле POST: query оседает в access-логах, а пара
    install_id+fp = креды к стеку (как ?token=)."""
    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if _rate_limited(ip, "refcode", 30, 60):
        return JSONResponse({"ok": False, "error": "too_many_attempts"}, status_code=429)
    body, err = await _read_capped(request, 2048)
    if err is not None:
        return err
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    iid = str(body.get("install_id") or "") if isinstance(body, dict) else ""
    fp = str(body.get("fp") or "") if isinstance(body, dict) else ""
    if not _INSTALL_ID_RE.match(iid):
        return JSONResponse({"ok": False, "error": "bad install_id"}, status_code=400)
    if fp and not _FP_RE.match(fp):
        return JSONResponse({"ok": False, "error": "bad fp"}, status_code=400)
    with _installs_lock:
        idata = _load_installs()
        irec = (idata or {}).get("installs", {}).get(iid) if idata else None
    if irec is None:
        return JSONResponse({"ok": False, "error": "unknown install"}, status_code=404)
    known_fp = str(irec.get("fp") or "")
    if known_fp and fp != known_fp:
        return JSONResponse({"ok": False, "error": "fp mismatch"}, status_code=403)
    _ref_sweep_pending()  # H1: возврат стеков с брошенных платежей (>24ч)
    code = _ref_code_for(iid)
    with _ref_lock:
        refs = _load_referrals()
        if refs is None:
            return JSONResponse({"ok": False, "error": "referrals store corrupted"}, status_code=500)
        rec = refs.get(code)
        if rec is None:
            # fp легаси-инсталла не дозаписываем из чужого вызова — иначе первый
            # звонящий навесил бы свой fp на чужой код
            rec = {"install_id": iid, "fp": known_fp, "created": now_iso(),
                   "clicks": 0, "buys": 0, "stack_n": 0, "history": []}
            refs[code] = rec
            _save_referrals(refs)
        elif rec.get("install_id") != iid:
            # коллизия 8-символьного кода двух install_id — чужую запись не перезаписываем
            log.error("ref code collision: %s", code)
            _event("ref_code_collision", code, iid)
            return JSONResponse({"ok": False, "error": "code collision"}, status_code=500)
    return JSONResponse({"ok": True, "code": code,
                         "link": f"{PUBLIC_BASE_URL}/?ref={code}",
                         "clicks": _safe_int(rec.get("clicks")),
                         "buys": _safe_int(rec.get("buys")),
                         "stack_n": _safe_int(rec.get("stack_n"))}, headers=_NOCACHE)


@mcp.custom_route("/api/referral", methods=["GET"])
async def api_referral(request: Request):
    """Инстанс: своя реф-карточка для дашборда («Система» → «Пригласи друга»).
    Прокси на магазин /api/referral/code с install_id+fp этого инстанса. Bearer —
    middleware (путь не публичный). Магазин недоступен → {ok:false} — блок скрыт,
    инстанс не ломается (fail-open, как весь hosted-контур)."""
    if STORE_MODE:
        return PlainTextResponse("Not Found", status_code=404)  # как /app: у магазина нет дашборда
    import asyncio
    iid = _install_id()
    fields = _client_fp_fields()
    # магазин сверяет fp с installs[iid].fp — там живёт effective fp (композитный при наличии)
    fp = fields.get("host_fp") or fields.get("fp") or ""
    out = {"ok": False, "install_id": iid, "fp": fp}
    try:
        # install_id/fp — в теле POST (query оседает в access-логах магазина)
        r = await asyncio.to_thread(_post_json, f"{PUBLIC_BASE_URL}/api/referral/code",
                                    {"install_id": iid, "fp": fp}, 8)
        if isinstance(r, dict) and r.get("ok"):
            out.update({k: r[k] for k in ("code", "link", "clicks", "buys", "stack_n") if k in r})
            out["ok"] = True
    except Exception:
        pass
    return JSONResponse(out, headers=_NOCACHE)


# ------------------------------------------------------------- billing ----

def _platega_create(plan_id: str, ref: str, amount: "int | None" = None) -> dict:
    """Создание платежа в Platega (X-MerchantId/X-Secret, v2 без метода — выбирает покупатель).
    amount=None → полная цена плана; продление приходит со скидкой от вызывающего."""
    import urllib.request
    plan = PLANS[plan_id]
    payload = {
        "paymentDetails": {"amount": amount if amount is not None else plan["RUB"], "currency": "RUB"},
        "description": plan["label"],
        "return": f"https://rezon-ai.ru/billing/result?ref={ref}",
        "failedUrl": "https://rezon-ai.ru/#pricing",
        "payload": ref,
    }
    req = urllib.request.Request(
        f"{PLATEGA_API}/v2/transaction/process",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-MerchantId": PLATEGA_MERCHANT_ID, "X-Secret": PLATEGA_API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


@mcp.custom_route("/api/plans", methods=["GET"])
async def api_plans(request: Request):
    """Публичный прайс для диалога покупки в дашборде. Секретов нет → CORS."""
    plans = [{"id": pid, "label": PLANS[pid]["label"], "rub": PLANS[pid]["RUB"],
              "usd": PLANS[pid]["USD"], "days": PLANS[pid]["days"]}
             for pid in ("pro1", "pro3", "pro6", "pro12") if pid in PLANS]
    return JSONResponse({"plans": plans}, headers={**_NOCACHE, **_CORS})


@mcp.custom_route("/api/plans", methods=["OPTIONS"])
@mcp.custom_route("/api/billing/pay", methods=["OPTIONS"])
@mcp.custom_route("/api/billing/status", methods=["OPTIONS"])
@mcp.custom_route("/api/billing/lava/invoice", methods=["OPTIONS"])
async def billing_cors_preflight(request: Request):
    """Preflight для кросс-ориджин покупки из дашборда."""
    return Response(status_code=204, headers=_CORS)


@mcp.custom_route("/api/billing/pay", methods=["POST"])
async def api_billing_pay(request: Request):
    """Кнопка «Купить» (лендинг/дашборд) → ссылка на оплату Platega.

    Опциональный extend=<текущий license key>: продление — вебхук прибавит дни
    плана к max(старый exp, сейчас), остаток не сгорает. Ключ проверяется сразу.
    Опциональный email (чек + recovery вебхуком): из тела (новая покупка) ИЛИ из
    подписанного payload extend-ключа (продление — источник доверенный)."""
    import asyncio
    import secrets
    ip = _client_ip(request)
    if _rate_limited(ip, "pay", 10, 60):
        return JSONResponse({"error": "too_many_attempts"}, status_code=429, headers=_CORS)
    if not (PLATEGA_MERCHANT_ID and PLATEGA_API_KEY):
        return JSONResponse({"error": "payments not configured"}, status_code=503, headers=_CORS)
    body, err = await _read_capped(request, 16384)
    if err is not None:
        return JSONResponse({"error": "bad request"}, status_code=400, headers=_CORS)
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400, headers=_CORS)
    plan_id = _plan_key(str(body.get("plan") or body.get("tier") or "pro1"))
    if not plan_id:
        return JSONResponse({"error": "plan must be pro1|pro3|pro6|pro12"}, status_code=400,
                            headers=_CORS)
    extend = str(body.get("extend") or "")[:600].strip()
    if extend and PLANS[plan_id].get("donation"):
        return JSONResponse({"error": "extend: продление неприменимо к донатам"}, status_code=400,
                            headers=_CORS)
    extend_email = ""
    if extend:
        if not extend.startswith("mg_"):
            return JSONResponse({"error": "extend: ожидается license key mg_…"}, status_code=400,
                                headers=_CORS)
        payload, err = licensing.verify_key(LICENSE_SECRET, extend)
        if err:
            return JSONResponse({"error": f"extend: {err}"}, status_code=400, headers=_CORS)
        if _is_revoked(_load_codes(), extend):
            return JSONResponse({"error": "extend: ключ отозван"}, status_code=400, headers=_CORS)
        extend_email = _valid_email(payload.get("email")) or ""  # подписанный, доверенный
    # email ОПЦИОНАЛЕН (совместимость со старыми дашбордами флота): из тела
    # (новая покупка) или из подписанного payload extend-ключа. Передан, но
    # мусорный → 400 (защита от опечаток); не передан → платёж без email
    # (ключ доедет поллингом /api/billing/status, но recovery по почте не сможет).
    if body.get("email") is not None and not _valid_email(body.get("email")):
        return JSONResponse({"error": "valid email required"}, status_code=400, headers=_CORS)
    email = extend_email or (_valid_email(body.get("email")) or "")
    # рефералка. body.ref — промокод (кука лендинга mg_ref или ручной ввод);
    # −20% только на НОВЫЙ платёж тарифа (не продление, не донат). Невалидный код →
    # полная цена без ошибки (fail-open для покупателя). shortcut: «первая покупка»
    # не форсится — email опционален (совместимость флота), проверять нечем.
    ref_code = "" if extend else _ref_lookup(body.get("ref"))
    # install_id/fp покупателя (дашборд-инстанс): self-ref блок в вебхуке + стек продления
    buyer_iid = str(body.get("install_id") or "")
    buyer_fp = str(body.get("fp") or "")
    buyer_iid = buyer_iid if _INSTALL_ID_RE.match(buyer_iid) else ""
    buyer_fp = buyer_fp if _FP_RE.match(buyer_fp) else ""
    # Скидки считаются ТОЛЬКО серверно (клиент цену не подсовывает):
    # продление −10% (EXTEND_DISCOUNT); со стеком рефералки — min(10+10×stack_n, 50)%
    # (стек включает базу). install_id валидируем мягко по
    # installs.json: неизвестный / fp не сошёлся → базовые −10%.
    pct = 0
    stack_code = ""
    burned_n = 0
    if extend:
        pct = round(100 * (1 - EXTEND_DISCOUNT))  # база −10%
        if buyer_iid:
            with _installs_lock:
                idata = _load_installs()
                irec = (idata or {}).get("installs", {}).get(buyer_iid) if idata else None
            # стек только при ПОДТВЕРЖДЁННОМ fp — легаси-инсталл без fp стек не применяет
            if irec is not None and irec.get("fp") and irec.get("fp") == buyer_fp:
                with _ref_lock:
                    refs = _load_referrals()
                    rrec = refs.get(_ref_code_for(buyer_iid)) if refs is not None else None
                    stack_n = _safe_int((rrec or {}).get("stack_n"))
                    # H1: стек СГОРАЕТ при создании платежа — иначе N параллельных
                    # продлений читают один стек (TOCTOU, мультитрата). Брошенный
                    # платёж вернёт стек: _ref_sweep_pending (24ч) / failed-вебхук.
                    if rrec is not None and stack_n > 0:
                        burned_n = stack_n
                        rrec["stack_n"] = 0
                        stack_code = _ref_code_for(buyer_iid)
                        rrec.setdefault("history", []).append(
                            {"ts": now_iso(), "amount": PLANS[plan_id]["RUB"],
                             "flagged": "stack_burn_at_create"})
                        rrec["history"] = rrec["history"][-200:]
                        _save_referrals(refs)
                    pct = min(10 + 10 * stack_n, 50)
        amount = int(round(PLANS[plan_id]["RUB"] * (100 - pct) / 100))
    elif ref_code and not PLANS[plan_id].get("donation"):
        amount = int(round(PLANS[plan_id]["RUB"] * REF_DISCOUNT))
    else:
        amount = None
    ref = "r-" + secrets.token_hex(8)
    try:
        resp = await asyncio.to_thread(_platega_create, plan_id, ref, amount=amount)
    except Exception as e:
        log.error("platega create failed: %s", e)
        _ref_restore_burned(stack_code, burned_n)  # N3: платёж не создан — стек назад
        return JSONResponse({"error": "payment creation failed"}, status_code=502, headers=_CORS)
    log.info("platega create response: %s", json.dumps(resp, ensure_ascii=False)[:500])
    pdata = resp.get("data") or resp
    pay_url = pdata.get("redirect") or pdata.get("url") or pdata.get("link") or pdata.get("paymentUrl")
    platega_id = str(pdata.get("id") or pdata.get("transactionId") or "")
    if not pay_url:
        # сырой ответ Platega наружу не отдаём (внутрянка) — детали в серверном логе
        _ref_restore_burned(stack_code, burned_n)  # N3
        return JSONResponse({"error": "no pay url"}, status_code=502, headers=_CORS)
    with _codes_lock:
        data = _load_codes()
        rec = {"id": ref, "ref": ref, "platega_id": platega_id, "plan": plan_id,
               "tier": "pro", "status": "created", "at": now_iso(), "payUrl": pay_url,
               "amount": amount if amount is not None else PLANS[plan_id]["RUB"]}
        if email:
            rec["email"] = email  # только когда известен — не засоряем запись пустотой
        if extend:
            rec["extend"] = extend
            if pct > 10:
                rec["extend_pct"] = pct  # стек применён и УЖЕ сгорел при создании (H1)
            if stack_code:
                rec["stack_code"] = stack_code
                rec["stack_ts"] = int(time.time())  # для _ref_sweep_pending (24ч)
        if ref_code:
            rec["ref_code"] = ref_code
        if buyer_iid:
            rec["buyer_install"] = buyer_iid
            if buyer_fp:
                rec["buyer_fp"] = buyer_fp
        data["payments"].append(rec)
        _save_codes(data)
    _event("payment_created", ref, f"{plan_id} · {amount if amount is not None else PLANS[plan_id]['RUB']}₽"
                                   + (f" · продление −{pct}%" if extend else "")
                                   + (f" · реф {ref_code} −20%" if ref_code else ""))
    return JSONResponse({"ok": True, "payUrl": pay_url, "ref": ref},
                        headers={**_NOCACHE, **_CORS})


@mcp.custom_route("/api/billing/status", methods=["GET", "POST"])
async def api_billing_status(request: Request):
    """Статус платежа по ref (bearer-секрет покупателя). После оплаты отдаёт
    licenseKey — дашборд  активирует его сам (автоцикл покупки).
    ref — в POST-теле (query string оседает в access-логах);
    GET ?ref= оставлен для старых страниц успеха."""
    ip = _client_ip(request)
    if _rate_limited(ip, "paystatus", 30, 60):
        return JSONResponse({"error": "too_many_attempts"}, status_code=429, headers=_CORS)
    ref = request.query_params.get("ref", "")[:64]
    if request.method == "POST":
        # мягкий кап: переросшее/битое тело просто не несёт ref — остаётся query-параметр
        body, err = await _read_capped(request, 4096)
        if err is None:
            try:
                ref = str(json.loads(body).get("ref") or "")[:64] or ref
            except Exception:
                pass
    data = _load_codes()
    p = next((x for x in data["payments"] if x.get("ref") == ref), None)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404, headers=_CORS)
    out = {"status": p.get("status"), "tier": p.get("tier"), "plan": p.get("plan")}
    if p.get("code"):
        out["code"] = p["code"]
    if p.get("licenseKey"):  # вебхук уже выписал ключ — отдаём владельцу ref
        out["licenseKey"] = p["licenseKey"]
    return JSONResponse(out, headers={**_NOCACHE, **_CORS})


@mcp.custom_route("/billing/result", methods=["GET"])
async def billing_result(request: Request):
    g = _store_gate()
    if g:
        return g
    return HTMLResponse(_page_html("billing.html"), headers=_NOCACHE)


# ----------------------------------------------------- license + purchase ---

@mcp.custom_route("/api/license", methods=["GET"])
async def api_license(request: Request):
    """Состояние лицензии/триала: {state, days_left, email?, tier?, exp?, latest?, update_available?}."""
    return JSONResponse(_license_status(), headers=_NOCACHE)


@mcp.custom_route("/api/version", methods=["GET"])
async def api_version(request: Request):
    """Публично: текущая версия этого инстанса (магазин отвечает своей — апдейт-чип).

    Осознанный tradeoff: точная версия публична → фингерпринтинг
    для будущих CVE. Принято: магазину публичность нужна, секретов тут нет.
    """
    return JSONResponse({"version": VERSION}, headers=_NOCACHE)


@mcp.custom_route("/api/update/trigger", methods=["POST"])
async def api_update_trigger(request: Request):
    """Кнопка «обновить сейчас» в дашборде: роняет пустой маркер
    DATA_DIR/.update-now; updater подхватывает его через docker exec и запускает
    цикл вне расписания (даже при AUTO_UPDATE=0). Содержимое маркера игнорируется —
    решение updater принимает сам по свежей подписанной версии магазина."""
    if not _admin_auth(request):
        return _admin_401()
    ip = _client_ip(request)
    if _rate_limited(ip, "updtrigger", 10, 300):
        return JSONResponse({"error": "too_many_attempts"}, status_code=429)
    try:
        tmp = os.path.join(DATA_DIR, ".update-now.tmp")
        with open(tmp, "w"):
            pass
        os.replace(tmp, os.path.join(DATA_DIR, ".update-now"))
    except OSError as e:
        log.error("update trigger: %s", e)
        return JSONResponse({"ok": False, "error": "trigger unavailable"}, status_code=500)
    latest = None
    try:
        v = _get_json(PUBLIC_BASE_URL + "/api/version", timeout=8)
        if isinstance(v, dict) and v.get("version"):
            latest = str(v["version"])
    except Exception:
        pass  # fail-open: свежесть определит updater; кнопка не зависит от сети до магазина
    return JSONResponse({"ok": True, "current": VERSION, "latest": latest}, headers=_NOCACHE)


def _release_file(suffix: str) -> Response:
    """Отдать файл релиза текущей VERSION из RELEASES_DIR (магазин).
    Нет файла → 404 (апдейтер трактует как «релиз ещё не упакован»)."""
    p = os.path.join(RELEASES_DIR, "maxgraph-%s.%s" % (VERSION, suffix))
    if suffix == "sha256":
        try:
            with open(p, "rb") as f:
                blob = f.read()
        except OSError:
            return JSONResponse({"ok": False, "error": "no release"}, status_code=404)
        return PlainTextResponse(blob.decode("ascii", "replace"),
                                 headers={"Cache-Control": "no-store"})
    if not os.path.isfile(p):
        return JSONResponse({"ok": False, "error": "no release"}, status_code=404)
    # FileResponse — стриминг (иначе файл целиком в RAM в async-хендлере —
    # бесплатная memory-амплификация на анонимном эндпоинте)
    media = "application/octet-stream" if suffix == "sig" else "application/gzip"
    return FileResponse(p, media_type=media,
                        headers={"Cache-Control": "no-store"})


def _release_guard(request: Request) -> "Response | None":
    """429 для раздачи релизов: анонимный мегабайтный эндпоинт на процессе биллинга."""
    ip = _client_ip(request)
    if _rate_limited(ip, "releases", 30, 60):
        return JSONResponse({"ok": False, "error": "too_many_requests"}, status_code=429)
    return None


@mcp.custom_route("/api/releases/latest.tar.gz", methods=["GET"])
async def api_release_tarball(request: Request):
    """Публично: tarball последнего релиза для автообновлений. Канал
    доставки — магазин (репо приватный: GitHub-архивы тегов требуют auth)."""
    guard = _release_guard(request)
    if guard is not None:
        return guard
    return _release_file("tar.gz")


@mcp.custom_route("/api/releases/latest.sha256", methods=["GET"])
async def api_release_sha(request: Request):
    """Публично: sha256 tarball'а (`<hex>  maxgraph-<ver>.tar.gz`, формат sha256sum -c)."""
    guard = _release_guard(request)
    if guard is not None:
        return guard
    return _release_file("sha256")


@mcp.custom_route("/api/releases/latest.sig", methods=["GET"])
async def api_release_sig(request: Request):
    """Публично: ed25519-подпись tarball'а. Trust anchor — публичный ключ,
    вшитый в образ updater'а при установке: компрометация раздачи (nginx/data) без
    ключа подписи (хранится только на сервере магазина) не даёт подсунуть код клиентам."""
    guard = _release_guard(request)
    if guard is not None:
        return guard
    return _release_file("sig")


# --- native releases: бинарники клиента без Docker ------------

# Имя строго по шаблону клиента (selfupdate.artifact_name) + sidecar-файлы
# + manifest.json для лендинга/скриптов. Всё прочее → 404 (не 400:
# невалидное имя неотличимо от «нет такого роута» — без оракула разведки).
_NATIVE_NAME_RE = re.compile(
    r"^(?:maxgraph-\d+\.\d+\.\d+-(?:windows|macos|linux)-(?:amd64|arm64)"
    r"(?:\.exe)?(?:\.sha256|\.sig)?|manifest\.json)$")


def _native_release_file(fname: str) -> Response:
    """Отдать native-артефакт из RELEASES_DIR/native.
    Нет файла → 404 (selfupdate трактует как «артефакта ещё нет», fail-open)."""
    p = os.path.join(RELEASES_DIR, "native", fname)
    if fname.endswith(".sha256"):
        # формат sha256sum (`<hex>  <name>`) — текст, как у latest.sha256
        try:
            with open(p, "rb") as f:
                blob = f.read()
        except OSError:
            return JSONResponse({"ok": False, "error": "no artifact"}, status_code=404)
        return PlainTextResponse(blob.decode("ascii", "replace"),
                                 headers={"Cache-Control": "no-store"})
    if not os.path.isfile(p):
        return JSONResponse({"ok": False, "error": "no artifact"}, status_code=404)
    if fname == "manifest.json":
        return FileResponse(p, media_type="application/json",
                            headers={"Cache-Control": "no-store"})
    if fname.endswith(".sig"):
        # ed25519 -rawin подпись — бинарь, как у latest.sig
        return FileResponse(p, media_type="application/octet-stream",
                            headers={"Cache-Control": "no-store"})
    # сам артефакт: стриминг (не читать целиком в RAM на анонимном роуте)
    return FileResponse(p, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store",
                                 "Content-Disposition": 'attachment; filename="%s"' % fname})


@mcp.custom_route("/api/releases/native/manifest", methods=["GET"])
async def api_release_native_manifest(request: Request):
    """Публично: манифест native-артефактов {"version", "artifacts":[...]}.
    Для лендинга/скриптов установки; 404 — манифест ещё не выложен."""
    guard = _release_guard(request)
    if guard is not None:
        return guard
    return _native_release_file("manifest.json")


@mcp.custom_route("/api/releases/native/{fname}", methods=["GET"])
async def api_release_native(request: Request):
    """Публично: native-бинарь и sidecar'ы (.sha256/.sig) для selfupdate.
    Канал доставки — магазин, trust anchor — вшитый в клиента публичный ключ."""
    guard = _release_guard(request)
    if guard is not None:
        return guard
    fname = request.path_params.get("fname", "")
    if not _NATIVE_NAME_RE.fullmatch(fname):  # $ в re пропускает \n в конце — fullmatch строже
        return JSONResponse({"ok": False, "error": "no artifact"}, status_code=404)
    return _native_release_file(fname)


@mcp.custom_route("/api/license/activate", methods=["POST"])
async def api_license_activate(request: Request):
    """Активация license key. Контракт: {key} → {ok:true,email,tier} | {ok:false,error}."""
    ip = _client_ip(request)
    if _rate_limited(ip, "activate", 10, 60):
        return JSONResponse({"ok": False, "error": "too_many_attempts"}, status_code=429)
    body, err = await _read_capped(request, 16384)
    if err is not None:
        return err
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if not isinstance(body, dict):  # "[1,2]" и т.п. — не 500, а 400
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    key = str(body.get("key") or "")[:512].strip()
    if not key:
        return JSONResponse({"ok": False, "error": "key required"}, status_code=400)
    try:
        import asyncio
        res = await asyncio.to_thread(_activate_key, key)  # внутри может быть сеть
    except Exception as e:  # fail-loud: активация не должна молча падать
        log.error("license activate error от %s: %s", ip, e)
        return JSONResponse({"ok": False, "error": f"activate failed: {e}"}, status_code=500)
    if not res.get("ok"):
        log.warning("license activate rejected от %s: %s", ip, res.get("error"))
        out = {"ok": False, "error": res["error"]}
        if res.get("machine_limit"):  # host-fp: лимит машин — отдельный сценарий, не «invalid»
            out["machine_limit"] = True
        return JSONResponse(out, headers=_NOCACHE)
    _event("license_activated", res.get("email") or "", res.get("tier", ""))
    log.info("license activated: %s · %s", res.get("tier"), res.get("email"))
    return JSONResponse({"ok": True, "email": res.get("email"), "tier": res.get("tier")}, headers=_NOCACHE)


def _activate_key(key: str) -> dict:
    """Активация — ТОЛЬКО онлайн-валидация на сервере магазина (verify-only).

    Локального приёма нет: без ok магазина ключ не активируется. Ключи магазина
    локально не проверяются (чужой секрет, by design). После активации лицензия
    полностью офлайн (license.json + watchdog раз в сутки, revoke → grace 72ч).
    Блокирующий сетевой вызов — вызывать в thread.

    host-fp: с ключом передаём fp машины (1 ключ = 1 машина). Ответ
    403 machine_limit — НЕ «ключ невалиден»: локальный state не трогаем,
    наружу отдаём machine_limit=True + понятный message.
    """
    if not PUBLIC_BASE_URL:
        return {"ok": False, "error": "активация требует связи с сервером магазина"}
    payload = {"key": key}
    payload.update(_client_fp_fields())
    payload["install_id"] = _install_id()  # связка ключ↔инстанс (self-ref блок рефералки)
    try:
        j = _post_json(f"{PUBLIC_BASE_URL}/api/license/validate", payload, timeout=6)
    except Exception as e:
        j = _http_error_json(e)  # штатный отказ с телом (403 machine_limit)
        if j is None:
            return {"ok": False, "error": f"сервер магазина недоступен — активация требует онлайн: {e}"}
    if not isinstance(j, dict):
        return {"ok": False, "error": "сервер магазина ответил не-JSON — активация отклонена"}
    if j.get("revoked"):  # defense-in-depth: отсечь даже битый {ok:true, revoked:true} магазина
        return {"ok": False, "error": "license revoked"}
    if not j.get("ok"):
        if j.get("error") == "machine_limit":
            return {"ok": False, "error": j.get("message") or _MACHINE_LIMIT_MSG,
                    "machine_limit": True}
        return {"ok": False, "error": j.get("error") or "сервер магазина отклонил ключ"}
    return licensing.grant(DATA_DIR, LICENSE_SECRET, j, key=key)


def _http_error_json(e: Exception):
    """HTTPError с JSON-телом → dict (штатные отказы магазина с телом: 403 machine_limit
    и т.п.); сетевая ошибка/не-JSON → None. urllib бросает HTTPError на любой не-2xx."""
    import urllib.error
    if not isinstance(e, urllib.error.HTTPError):
        return None
    try:
        j = json.loads(e.read(65536).decode("utf-8"))  # кап, как у _post_json
    except Exception:
        return None
    return j if isinstance(j, dict) else None


@mcp.custom_route("/api/license/validate", methods=["POST"])
async def api_license_validate(request: Request):
    """Сервер магазина: проверка ключа self-hosted установками покупателей.

    {key, fp?} → {ok:true,email,tier,exp} | {ok:false,error}. Только валидация
    подписи магазина — без выдачи новых ключей. Публичный, rate-limit.

    host-fp: клиент передаёт fp (sha256-хеш машины). fp известен → ok; fp новый
    и свободный слот (≤2 на ключ) → привязка + ok; слоты заняты → 403
    machine_limit (состояние ключа у клиента НЕ затирается — отдельный сценарий
    с понятным текстом). Без fp — legacy fail-open: ok, привязок не создаём.
    """
    ip = _client_ip(request)
    if _rate_limited(ip, "license_validate", 20, 60):
        return JSONResponse({"ok": False, "error": "too_many_attempts"}, status_code=429)
    body, err = await _read_capped(request, 16384)
    if err is not None:
        return err
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if not isinstance(body, dict):  # "[1,2]" и т.п. — не 500, а 400
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    key = str(body.get("key") or "")[:512].strip()
    payload, err = licensing.verify_key(LICENSE_SECRET, key)
    if err:
        return JSONResponse({"ok": False, "error": err}, headers=_NOCACHE)
    rec = _is_revoked(_load_codes(), key)  # реестр отзывов (чарджбэк/фрод)
    if rec:
        _event("license_validate_revoked", payload.get("email") or "", payload.get("tier", ""))
        return JSONResponse({"ok": False, "revoked": True, "error": "license revoked",
                             "revoked_at": rec.get("ts") or 0}, headers=_NOCACHE)
    fp = str(body.get("fp") or "")[:128].strip().lower()
    host_fp, components, fp_kind = _parse_hostfp_body(body)
    eff = host_fp or fp  # effective id машины: композитный при наличии, иначе legacy
    if fp and not _FP_RE.match(fp):
        return JSONResponse({"ok": False, "error": "bad fp"}, status_code=400)
    if eff and fp_kind != "container":
        # container-fp (unanchored-установка) слот не занимает: привязка создаётся,
        # когда появится настоящий fp; до этого — legacy fail-open, ключ работает
        try:
            with _machines_lock:
                mdata = _load_machines()
                verdict, _rec = _machine_bind(mdata, key, eff, time.time(),
                                              components=components, legacy_fp=fp,
                                              email=payload.get("email") or "")
                if verdict != "limit":
                    _save_machines(mdata)
        except Exception:  # битый machines.json → fail-closed (500), привязки целы
            log.error("validate: machines.json не читается — отказ (fail-closed)")
            return JSONResponse({"ok": False, "error": "store registry error"}, status_code=500)
        if verdict == "limit":
            _event("license_machine_limit", payload.get("email") or "", eff[:12])
            log.info("validate: machine_limit для %s (fp=%s…)",
                     payload.get("email") or "?", eff[:8])
            return JSONResponse({"ok": False, "error": "machine_limit",
                                 "message": _MACHINE_LIMIT_MSG},
                                status_code=403, headers=_NOCACHE)
        if verdict == "bound":
            _event("license_machine_bound", payload.get("email") or "", eff[:12])
        elif verdict == "rebound":
            # fp заменён по доказанному тождеству машины (legacy→composite или
            # скоринг компонентов); старый fp — алиас, слот не расходуется
            _event("license_machine_rebound", payload.get("email") or "", eff[:12])
        elif verdict == "review":
            # то же, но скоринг в серой зоне — флаг review виден только в админке
            _event("license_machine_review", payload.get("email") or "", eff[:12])
    fp = eff  # дальше (key_installs, self-ref) работает effective fp
    # инстанс присылает install_id → связка ключ↔инстанс (key_installs в
    # codes.json) для self-ref блока рефералки: ключ куплен по реф-коду и активируется
    # с инстанса владельца кода → откат начисления (лицензию не трогаем — она оплачена).
    iid = str(body.get("install_id") or "")
    iid = iid if _INSTALL_ID_RE.match(iid) else ""
    selfref_msg = None
    if iid:
        with _codes_lock:
            data = _load_codes()
            ki = data.setdefault("key_installs", {})
            kh = _key_hash(key)
            prev = ki.get(kh) or {}
            if prev.get("install_id") != iid or prev.get("fp") != fp:
                ki[kh] = {"install_id": iid, "fp": fp, "at": now_iso()}
                if len(ki) > 5000:  # потолок реестра, как у revoked: трим самых старых
                    data["key_installs"] = dict(sorted(ki.items(),
                                                       key=lambda kv: kv[1].get("at", ""))[-4000:])
                pay = next((p for p in data["payments"]
                            if p.get("licenseKey") == key and p.get("ref_code")), None)
                if pay is not None and not pay.get("selfref"):
                    rcode = str(pay["ref_code"])
                    with _ref_lock:  # порядок _codes_lock → _ref_lock, как в вебхуке
                        refs = _load_referrals()
                        rrec = refs.get(rcode) if refs is not None else None
                        if rrec and (rrec.get("install_id") == iid
                                     or (fp and rrec.get("fp") and rrec.get("fp") == fp)):
                            pay["selfref"] = True
                            rrec["buys"] = max(0, _safe_int(rrec.get("buys")) - 1)
                            rrec["stack_n"] = max(0, _safe_int(rrec.get("stack_n")) - 1)
                            rrec.setdefault("history", []).append(
                                {"ts": now_iso(), "payment_id": pay.get("ref") or pay.get("id"),
                                 "amount": pay.get("amount"), "flagged": "self_ref_activation"})
                            rrec["history"] = rrec["history"][-200:]
                            _save_referrals(refs)
                            selfref_msg = (f"⚠️ Self-ref при активации: код {rcode} · "
                                           f"начисление откачено")
                            _event("ref_self_activation", rcode, iid)
                _save_codes(data)
    if selfref_msg and TG_NOTIFY_CHAT_ID:  # сеть — после локов, best-effort
        tg_token = os.environ.get("TG_BOT_TOKEN", "")
        if tg_token:
            import asyncio
            try:
                await asyncio.to_thread(billing.send_telegram, TG_NOTIFY_CHAT_ID, selfref_msg, tg_token)
            except Exception as e:
                log.error("ref self-ref telegram: %s", e)
    _event("license_validated", payload.get("email") or "", payload.get("tier", ""))
    return JSONResponse({"ok": True, "email": payload.get("email"),
                         "tier": payload.get("tier"), "exp": payload.get("exp") or 0},
                        headers=_NOCACHE)


@mcp.custom_route("/api/tenants", methods=["GET"])
async def api_tenants_list(request: Request):
    """Учёт активаций для админки: tenant ↔ email ↔ код ↔ licenseKey,
    флаг revoked. Bearer master ИЛИ admin-сессия (cookie, STORE_MODE).
    блок «Пользователи» — к каждому tenant его машины из machines.json
    (по licenseKey), expires из payload ключа (0 = бессрочно), плюс unattached —
    привязки, не сматчившиеся ни с одним tenant.
    джойн по EMAIL записи машины (перевыпуск ключа меняет key_hash,
    машина не должна выпадать в unattached); фолбэк для легаси-записей без
    email — старое поведение по key_hash."""
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    data = _load_codes()
    try:  # джойн machines.json — best-effort (как /api/installs): битый реестр не роняет выдачу
        mregs = _load_machines().get("machines") or {}
    except Exception:
        mregs = {}
    # MINOR-1: пред-индекс по email — джойн O(tenants + machines) вместо O(tenants × machines)
    by_email: dict = {}
    for mkh, mrec in mregs.items():
        m_email = str(mrec.get("email") or "").strip().lower()
        if m_email:
            by_email.setdefault(m_email, []).append((mkh, mrec))
    rows = []
    matched_kh = set()
    for t in data.get("tenants", []):
        lk = t.get("licenseKey") or ""
        t_email = str(t.get("email") or "").strip().lower()
        kh = _key_hash(lk) if lk else ""
        machines, exp = [], None
        candidates = by_email.get(t_email, []) if t_email else []
        if kh and kh in mregs:
            candidates = candidates + [(kh, mregs[kh])]
        seen_kh = set()  # запись может попасть в кандидаты и по email, и по key_hash
        for mkh, mrec in candidates:
            if mkh in seen_kh:
                continue
            seen_kh.add(mkh)
            matched_kh.add(mkh)
            fp_class = "composite" if mrec.get("components") else "legacy"
            machines.extend({"fp": str(fp)[:8], "fp_class": fp_class,
                             "first_seen": mrec.get("first_seen"),
                             "updated": mrec.get("updated"),
                             "review": bool(mrec.get("review"))}
                            for fp in mrec.get("fps") or [])
        if lk:
            payload, _e = licensing.verify_key(LICENSE_SECRET, lk, now=0)  # now=0: истёкший тоже читаем
            if payload:
                exp = int(payload.get("exp") or 0)
        rows.append({"id": t.get("id"), "email": t.get("email"), "tier": t.get("tier"),
                     "code": t.get("code"), "createdAt": t.get("createdAt"),
                     "paidAt": t.get("paidAt"), "licenseKey": lk, "expires": exp,
                     "machines": machines,
                     "revoked": bool(lk) and bool(_is_revoked(data, lk))})
    unattached = []
    for kh, mrec in mregs.items():
        if kh in matched_kh:
            continue
        for fp in mrec.get("fps") or []:
            u = {"fp": str(fp)[:8], "first_seen": mrec.get("first_seen"),
                 "updated": mrec.get("updated")}
            if mrec.get("email"):  # помогает опознать сироту в админке
                u["email"] = mrec["email"]
            unattached.append(u)
    rows.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
    return JSONResponse({"tenants": rows[:500], "unattached": unattached[:500]},
                        headers=_NOCACHE)


# ------------------------------------------------------------------ admin auth ----
# Вход владельца магазина по логину+паролю (не master-токен в localStorage).
# Пароль — pbkdf2-hmac-sha256 (200k итераций, соль 16B) в data/admin.json (chmod 600).
# Сессия 12ч — httpOnly-кука mg_admin (SameSite=Lax; Secure на https); хранилище сессий
# in-memory: рестарт процесса = перелогин (осознанно). Перебор: 5 попыток / 15 мин на IP.
# Self-hosted ничего не меняет: там Bearer MEMORY_TOKEN, куки игнорируются.

_ADMIN_FILE = DATA_DIR / "admin.json"
_ADMIN_SESSIONS: dict = {}          # sha256(session_token) -> expiry epoch
_ADMIN_SESSION_TTL = 12 * 3600
_ADMIN_PBKDF2_ROUNDS = 200_000
_ADMIN_COOKIE_PATHS = {"/api/payments", "/api/tenants", "/api/codes", "/api/codes/generate",
                       "/api/codes/export", "/api/installs", "/api/admin/revoke",
                       "/api/admin/logout", "/api/admin/password",
                       "/api/admin/machines", "/api/admin/machines/unbind",  # host-fp
                       "/api/admin/support-reports",  # вкладка «Баги» (GET отчётов)
                       "/api/admin/stats"}            # вкладка «Статистика» (агрегация)


def _admin_cfg() -> dict | None:
    """{} = файла нет (setup открыт); None = файл есть, но не читается/не dict —
    fail-closed: setup/login/password → 500, configured считаем True,
    иначе битый файл молча переоткрывает публичный enrollment (takeover)."""
    if not _ADMIN_FILE.exists():
        return {}
    try:
        cfg = json.loads(_ADMIN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    return cfg if isinstance(cfg, dict) else None


def _admin_cfg_save(cfg: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _ADMIN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_ADMIN_FILE)
    try:
        os.chmod(_ADMIN_FILE, 0o600)
    except OSError:
        pass  # Windows — best-effort


def _admin_set_password(cfg: dict, user: str, password: str) -> dict:
    salt = secrets.token_bytes(16)
    return {"user": user, "salt": salt.hex(),
            "pass_hash": hashlib.pbkdf2_hmac("sha256", password.encode(), salt,
                                             _ADMIN_PBKDF2_ROUNDS).hex()}


def _admin_check_password(user: str, password: str, cfg: dict) -> bool:
    """Сверка user+password без timing-оракула: pbkdf2 считается всегда (при наличии
    salt/hash), раннего выхода по user нет. Кривой hex (ручная правка) → False, не 500."""
    ok_user = hmac.compare_digest(str(cfg.get("user", "")), user)
    salt_hex, expect = cfg.get("salt", ""), cfg.get("pass_hash", "")
    if not salt_hex or not expect:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt,
                              _ADMIN_PBKDF2_ROUNDS).hex()
    return ok_user and hmac.compare_digest(got, expect)


def _admin_session_ok_cookie(cookie_header: str) -> bool:
    tok = ""
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "mg_admin":
            tok = v
            break
    if not tok:
        return False
    h = hashlib.sha256(tok.encode()).hexdigest()
    exp = _ADMIN_SESSIONS.get(h)
    if not exp or exp < time.time():
        _ADMIN_SESSIONS.pop(h, None)
        return False
    return True


def _admin_session_ok(request) -> bool:
    return _admin_session_ok_cookie(request.headers.get("cookie") or "")


def _admin_auth(request) -> bool:
    """Админ-доступ к данным: Bearer master-токен (скрипты) ИЛИ сессионная кука (магазин)."""
    auth = request.headers.get("authorization", "")
    if TOKEN and hmac.compare_digest(auth, f"Bearer {TOKEN}"):
        return True
    return STORE_MODE and _admin_session_ok(request)


def _admin_401():
    return JSONResponse({"error": "unauthorized"}, status_code=401, headers=_NOCACHE)


# login-лимит считает ТОЛЬКО неудачи: успешный вход чистит бакет.
def _rate_peek(ip: str, bucket: str, maxn: int, window: int) -> bool:
    now = time.time()
    hits = [t for t in _rate_buckets.get(f"{bucket}:{ip}", []) if now - t < window]
    return len(hits) >= maxn


def _rate_hit(ip: str, bucket: str):
    _rate_buckets.setdefault(f"{bucket}:{ip}", []).append(time.time())


def _rate_clear(ip: str, bucket: str):
    _rate_buckets.pop(f"{bucket}:{ip}", None)


_ADMIN_SETUP_LOCK = threading.Lock()  # check-and-save атомарно (страховка)


@mcp.custom_route("/api/admin/me", methods=["GET"])
async def api_admin_me(request: Request):
    """Состояние для страницы входа: настроен ли пароль, залогинен ли. Публичное.
    Битый admin.json → configured=true (fail-closed: enrollment остаётся закрытым)."""
    g = _store_gate()
    if g:
        return g
    cfg = _admin_cfg()
    configured = cfg is None or bool(cfg.get("pass_hash"))
    authed = _admin_auth(request)
    if not authed and configured:
        # без сессии не раскрываем даже факт настроенности (скрытие поверхности);
        # первичная настройка (configured=false) отвечает — иначе enrollment не пройти
        return PlainTextResponse("Not Found", status_code=404)
    return JSONResponse({"configured": configured, "authed": authed}, headers=_NOCACHE)


@mcp.custom_route("/api/admin/setup", methods=["POST"])
async def api_admin_setup(request: Request):
    """Первичная настройка пароля — ровно один раз (дальше 409). Только магазин.
    Закрыта master-токеном (Bearer): иначе любой в интернете настроит пароль раньше
    владельца (публичный first-come enrollment)."""
    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if _rate_limited(ip, "adminsetup", 5, 900):
        return JSONResponse({"error": "too_many_attempts"}, status_code=429)
    if not _admin_auth(request):
        return _admin_401()
    body, err = await _read_capped(request, 4096)
    if err is not None:
        return JSONResponse({"error": "bad request"}, status_code=400)
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    cfg = _admin_cfg()
    if cfg is None:
        log.error("admin: admin.json повреждён — setup отклонён (fail-closed)")
        return JSONResponse({"error": "config corrupted"}, status_code=500)
    user = str(body.get("user") or "")[:32].strip()
    password = str(body.get("password") or "")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{3,32}", user):
        return JSONResponse({"error": "user: 3-32 символа [a-z0-9_.-]"}, status_code=400)
    if len(password) < 8:
        return JSONResponse({"error": "password: минимум 8 символов"}, status_code=400)
    with _ADMIN_SETUP_LOCK:
        if _admin_cfg().get("pass_hash"):
            return JSONResponse({"error": "already configured"}, status_code=409)
        _admin_cfg_save(_admin_set_password({}, user, password))
    _event("admin_password_set", user, "")
    log.info("admin: пароль настроен (user=%s)", user)
    return JSONResponse({"ok": True}, headers=_NOCACHE)


@mcp.custom_route("/api/admin/login", methods=["POST"])
async def api_admin_login(request: Request):
    """Логин → httpOnly-сессия на 12ч. Лимит 5 неудач / 15 мин на IP (успех бакет чистит)."""
    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if _rate_peek(ip, "adminlogin", 5, 900):
        return JSONResponse({"error": "too_many_attempts"}, status_code=429)
    body, err = await _read_capped(request, 4096)
    if err is not None:
        return JSONResponse({"error": "bad request"}, status_code=400)
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    user = str(body.get("user") or "")[:64].strip()
    password = str(body.get("password") or "")
    cfg = _admin_cfg()
    if cfg is None:
        return JSONResponse({"error": "config corrupted"}, status_code=500)
    if not cfg.get("pass_hash"):
        return JSONResponse({"error": "not configured"}, status_code=409)
    if not _admin_check_password(user, password, cfg):
        _rate_hit(ip, "adminlogin")
        # user санитизируем: log-forging через \n и пароль, вставленный в поле логина
        log.warning("admin: неудачный вход user=%s ip=%s",
                    re.sub(r"[^\w.@-]", "?", user), ip)
        return JSONResponse({"error": "неверный логин или пароль"}, status_code=401)
    _rate_clear(ip, "adminlogin")
    tok = secrets.token_urlsafe(32)
    _ADMIN_SESSIONS[hashlib.sha256(tok.encode()).hexdigest()] = time.time() + _ADMIN_SESSION_TTL
    secure = "; Secure" if request.url.scheme == "https" else ""
    cookie = (f"mg_admin={tok}; Path=/; HttpOnly; SameSite=Lax; "
              f"Max-Age={_ADMIN_SESSION_TTL}{secure}")
    _event("admin_login", user, "")
    return JSONResponse({"ok": True}, headers={**_NOCACHE, "Set-Cookie": cookie})


@mcp.custom_route("/api/admin/logout", methods=["POST"])
async def api_admin_logout(request: Request):
    g = _store_gate()
    if g:
        return g
    tok = ""
    for part in (request.headers.get("cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "mg_admin":
            tok = v
            break
    if tok:
        _ADMIN_SESSIONS.pop(hashlib.sha256(tok.encode()).hexdigest(), None)
    return JSONResponse({"ok": True},
                        headers={**_NOCACHE, "Set-Cookie": "mg_admin=; Path=/; HttpOnly; Max-Age=0"})


@mcp.custom_route("/api/admin/password", methods=["POST"])
async def api_admin_password(request: Request):
    """Смена пароля (текущая сессия). Старые сессии сбрасываются."""
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    body, err = await _read_capped(request, 4096)
    if err is not None:
        return JSONResponse({"error": "bad request"}, status_code=400)
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    cfg = _admin_cfg()
    if cfg is None:
        return JSONResponse({"error": "config corrupted"}, status_code=500)
    if not _admin_check_password(cfg.get("user", ""), str(body.get("old") or ""), cfg):
        return JSONResponse({"error": "текущий пароль неверен"}, status_code=401)
    new = str(body.get("new") or "")
    if len(new) < 8:
        return JSONResponse({"error": "password: минимум 8 символов"}, status_code=400)
    _admin_cfg_save(_admin_set_password({}, cfg.get("user", "admin"), new))
    _ADMIN_SESSIONS.clear()  # пароль сменили → все сессии мертвы
    _event("admin_password_changed", cfg.get("user", ""), "")
    return JSONResponse({"ok": True}, headers={**_NOCACHE,
                                               "Set-Cookie": "mg_admin=; Path=/; HttpOnly; Max-Age=0"})


@mcp.custom_route("/api/admin/revoke", methods=["POST"])
async def api_admin_revoke(request: Request):
    """Отзыв/восстановление license key (анти-чарджбэк). Bearer-only.

    {key} → revoke; {key, unrevoke:true} → снять отзыв. В реестре — sha256 ключа
    + серверный ts (якорь grace 72ч на self-hosted). Подпись ключа проверяется,
    просроченные тоже можно (now=0: exp-чек выключен, подпись проверяется).
    Доступ: Bearer master ИЛИ admin-сессия (cookie, STORE_MODE)."""
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    body, err = await _read_capped(request, 16384)
    if err is not None:
        return err
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if not isinstance(body, dict):  # "[1,2]" и т.п. — не 500, а 400
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    key = str(body.get("key") or "")[:600].strip()
    unrevoke = bool(body.get("unrevoke"))
    if not key.startswith("mg_"):
        return JSONResponse({"ok": False, "error": "key must start with mg_"}, status_code=400)
    payload, err = licensing.verify_key(LICENSE_SECRET, key, now=0)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    with _codes_lock:
        data = _load_codes()
        lst = data.setdefault("revoked", [])
        if unrevoke:
            n = len(lst)
            lst[:] = [r for r in lst if not hmac.compare_digest(str(r.get("h", "")), _key_hash(key))]
            changed = len(lst) != n
        else:
            changed = not _is_revoked(data, key)
            if changed:
                pay = next((p for p in data.get("payments", []) if p.get("licenseKey") == key), {})
                lst.append({"h": _key_hash(key), "ts": time.time(),
                            "note": (pay.get("email") or payload.get("email") or "")[:254]})
        if changed:
            _save_codes(data)
    _event("license_unrevoked" if unrevoke else "license_revoked",
           payload.get("email") or "", key[:10] + "…")
    return JSONResponse({"ok": True, "revoked": not unrevoke, "changed": changed},
                        headers=_NOCACHE)


@mcp.custom_route("/api/admin/machines", methods=["POST"])
async def api_admin_machines(request: Request):
    """Привязки машин ключа (host-fp): {key} → {fps, first_seen, updated, limit}.
    Для админки /admin (владелец магазина). Неизвестный ключ → пустой список.
    POST, а не GET: license key не должен оседать в access-логах uvicorn/nginx
    (тот же мотив, по которому revoke берёт ключ из тела)."""
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    body, err = await _read_capped(request, 16384)
    if err is not None:
        return err
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    key = str(body.get("key") or "")[:600].strip()
    if not key.startswith("mg_"):
        return JSONResponse({"ok": False, "error": "key must start with mg_"}, status_code=400)
    try:
        data = _load_machines()
    except Exception:
        return JSONResponse({"ok": False, "error": "machines registry corrupted"}, status_code=500)
    rec = (data.get("machines") or {}).get(_key_hash(key)) or {}
    return JSONResponse({"ok": True, "fps": list(rec.get("fps") or []),
                         "aliases": list(rec.get("aliases") or []),
                         "review": bool(rec.get("review")),
                         "fp_class": "composite" if rec.get("components") else "legacy",
                         "first_seen": rec.get("first_seen"), "updated": rec.get("updated"),
                         "limit": _MACHINES_MAX_FP}, headers=_NOCACHE)


@mcp.custom_route("/api/admin/machines/unbind", methods=["POST"])
async def api_admin_machines_unbind(request: Request):
    """Отвязка машины от ключа — ТОЛЬКО владельцем (переноса самообслуживанием
    нет). {key, fp} → удалить fp из привязок ключа. Мутация
    только POST (SameSite=Lax кука на кросс-сайт POST fetch не едет — CSRF-паттерн
    как у /api/codes/export mark=1). Подпись ключа проверяем, как в revoke."""
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    body, err = await _read_capped(request, 16384)
    if err is not None:
        return err
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    key = str(body.get("key") or "")[:600].strip()
    fp = str(body.get("fp") or "")[:128].strip().lower()
    if not key.startswith("mg_"):
        return JSONResponse({"ok": False, "error": "key must start with mg_"}, status_code=400)
    if not _FP_RE.match(fp):
        return JSONResponse({"ok": False, "error": "bad fp"}, status_code=400)
    payload, kerr = licensing.verify_key(LICENSE_SECRET, key, now=0)  # как revoke: now=0
    if kerr:
        return JSONResponse({"ok": False, "error": kerr}, status_code=400)
    try:
        with _machines_lock:
            data = _load_machines()
            rec = (data.get("machines") or {}).get(_key_hash(key))
            fps = (rec or {}).get("fps") or []
            aliases = (rec or {}).get("aliases") or []
            changed = fp in fps or fp in aliases
            if changed:
                if fp in fps:
                    rec["fps"] = [x for x in fps if x != fp]
                    if not rec["fps"]:  # каноничный fp ушёл — его components/review/aliases
                        rec.pop("components", None)   # не живут без него: иначе старая машина
                        rec.pop("review", None)       # проходила бы validate по алиасу БЕЗ слота,
                        rec["aliases"] = []           # а слот достался бы второй машине
                if fp in aliases:
                    rec["aliases"] = [x for x in aliases if x != fp]
                rec["updated"] = time.time()
                _save_machines(data)
    except Exception:
        return JSONResponse({"ok": False, "error": "machines registry corrupted"}, status_code=500)
    if changed:
        _event("license_machine_unbound", payload.get("email") or "", fp[:12])
        log.info("admin: отвязана машина fp=%s… от ключа %s", fp[:8], payload.get("email") or "?")
    return JSONResponse({"ok": True, "changed": changed, "fps": (rec or {}).get("fps") or []},
                        headers=_NOCACHE)


@mcp.custom_route("/billing/create", methods=["POST"])
async def billing_create(request: Request):
    """Контракт фронта: {plan:"pro1"|"pro3"|"pro6"|"pro12" (легаси: tier:"pro"), email} → 200 {payment_url} | 4xx/503 {error}."""
    import asyncio
    import secrets
    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if _rate_limited(ip, "billing_create", 10, 60):
        return JSONResponse({"error": "too_many_attempts"}, status_code=429)
    if not (PLATEGA_MERCHANT_ID and PLATEGA_API_KEY):
        return JSONResponse({"error": "payments not configured (PLATEGA_MERCHANT_ID/PLATEGA_API_KEY)"},
                            status_code=503)
    try:
        raw, _err = await _read_capped(request, 16384)  # кап по факту и тут
        if _err is not None:
            return JSONResponse({"error": "bad request"}, status_code=400)
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not isinstance(body, dict):  # [1,2] и т.п. не ломают .get
        return JSONResponse({"error": "bad json"}, status_code=400)
    plan_id = _plan_key(str(body.get("plan") or body.get("tier") or "")[:16])
    email = _valid_email(body.get("email")) or ""
    if not plan_id:
        return JSONResponse({"error": "plan must be pro1|pro3|pro6|pro12|donate99|donate249|donate499"}, status_code=400)
    if not email:
        return JSONResponse({"error": "valid email required"}, status_code=400)
    # промокод рефералки (кука mg_ref → buybox шлёт body.ref) — −20% на
    # платёж тарифа (донатам фикс-цена). Невалидный код → полная цена, без ошибки.
    # shortcut: «первая покупка» по email не проверяется (антифрод по email).
    ref_code = "" if PLANS[plan_id].get("donation") else _ref_lookup(body.get("ref"))
    amount = None
    if ref_code:
        base_price = PLANS[plan_id].get(PLATEGA_CURRENCY) or PLANS[plan_id]["RUB"]
        amount = int(round(base_price * REF_DISCOUNT))
    ref = "r-" + secrets.token_hex(8)
    try:
        pay = await asyncio.to_thread(
            billing.create_platega_payment,
            PLATEGA_MERCHANT_ID, PLATEGA_API_KEY, plan_id, email,
            f"{PUBLIC_BASE_URL}/billing/success?pid={ref}",
            PLATEGA_API, PLATEGA_CURRENCY, f"{PUBLIC_BASE_URL}/#pricing", ref,
            amount=amount)
    except billing.BillingError as e:
        log.error("billing/create failed: %s", e)
        # N4: str(e) может нести сырой ответ Platega — наружу generic, детали в логе
        return JSONResponse({"error": "payment creation failed"}, status_code=502)
    with _codes_lock:
        data = _load_codes()
        # email храним у себя: callback Platega его не несёт (docs.platega.io CallbackPayload)
        rec = {"id": ref, "ref": ref, "platega_id": pay["payment_id"],
               "plan": plan_id, "tier": "donation" if PLANS[plan_id].get("donation") else "pro",
               "email": email, "status": "created",
               "at": now_iso(), "payUrl": pay["payment_url"]}
        if ref_code:
            rec["ref_code"] = ref_code
            rec["amount"] = amount  # ожидание для сверки суммы в вебхуке
        data["payments"].append(rec)
        _save_codes(data)
    _event("payment_created", ref, f"{plan_id} · {email}")
    return JSONResponse({"payment_url": pay["payment_url"]}, headers=_NOCACHE)


# --- lava.top: EN-витрина (USD, иностранные карты) ---

_LAVA_PLANS = {"1": "pro1", "3": "pro3", "6": "pro6", "12": "pro12"}
# сумма USD → план (fallback в вебхуке, когда запись платежа вытеснена тримом 500).
# pro1 ($5) ИСКЛЮЧЁН: $5 доната == $5 pro1 — коллизия неразрешима, fail closed
# (lava_unknown_amount + ручная выдача), иначе донат дал бы ключ (security MED-1).
_LAVA_PLAN_BY_AMOUNT = {float(PLANS[p]["USD"]): p for p in ("pro3", "pro6", "pro12")}
# EN-донаты (поддержка, без ключа): белый список сумм USD, решение 2026-10-19.
# В fallback по сумме НЕ входят: угадывать донат по сумме нельзя (вытесненная
# запись → lava_unknown_amount, ключ не выдаётся).
_LAVA_DONATE = {"d5": 5, "d10": 10, "d25": 25}


@mcp.custom_route("/api/billing/lava/invoice", methods=["POST"])
async def api_billing_lava_invoice(request: Request):
    """EN-флоу лендинга и дашборда: {plan: 1|3|6|12|d5|d10|d25, email} → {paymentUrl} (lava.top, USD).

    Цена всегда серверная (PLANS[plan]["USD"] / _LAVA_DONATE) — amount от клиента
    не принимаем. d5/d10/d25 — донаты (tier=donation, без ключа в вебхуке).
    Платёж ждёт вебхук /api/webhooks/lava (матч по lava_id = contractId инвойса).
    CORS: дашборд покупателя (localhost) шлёт cross-origin POST — как /api/billing/pay.
    """
    import asyncio
    import secrets
    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if _rate_limited(ip, "lava_invoice", 10, 60):
        return JSONResponse({"error": "too_many_attempts"}, status_code=429, headers=_CORS)
    if not (LAVA_API_KEY and LAVA_OFFER_ID and lava):
        return JSONResponse({"error": "payments not configured"}, status_code=503, headers=_CORS)
    raw, err = await _read_capped(request, 16384)
    if err is not None:
        return JSONResponse({"error": "bad request"}, status_code=400, headers=_CORS)
    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400, headers=_CORS)
    if not isinstance(body, dict):  # [1,2] и т.п. не ломают .get
        return JSONResponse({"error": "bad json"}, status_code=400, headers=_CORS)
    plan_key = str(body.get("plan") or "")[:4]
    plan_id = _LAVA_PLANS.get(plan_key)
    donation = plan_key in _LAVA_DONATE
    if not plan_id and not donation:
        return JSONResponse({"error": "plan must be 1|3|6|12|d5|d10|d25"}, status_code=400, headers=_CORS)
    email = _valid_email(body.get("email"))
    if not email:
        return JSONResponse({"error": "valid email required"}, status_code=400, headers=_CORS)
    ref = "r-" + secrets.token_hex(8)
    amount = _LAVA_DONATE[plan_key] if donation else PLANS[plan_id]["USD"]
    try:
        pay = await asyncio.to_thread(
            lava.create_invoice, LAVA_API_KEY, LAVA_OFFER_ID, amount, email,
            f"{PUBLIC_BASE_URL}/billing/success?pid={ref}",
            f"{PUBLIC_BASE_URL}/en/#{'donate' if donation else 'pricing'}", LAVA_API)
    except Exception as e:  # BillingError: детали (сырой ответ lava) в лог, наружу generic
        log.error("lava invoice failed: %s", e)
        return JSONResponse({"error": "payment creation failed"}, status_code=502, headers=_CORS)
    # defense-in-depth (LOW-2): отдаём клиенту редирект только на lava.top
    from urllib.parse import urlparse
    _host = (urlparse(pay["payment_url"]).hostname or "")
    if not (pay["payment_url"].startswith("https://")
            and (_host == "lava.top" or _host.endswith(".lava.top"))):
        log.error("lava invoice: неожиданный payment_url host %r — не отдаём клиенту", _host)
        return JSONResponse({"error": "payment creation failed"}, status_code=502, headers=_CORS)
    rec = {"id": ref, "ref": ref, "lava_id": pay["payment_id"],
           "plan": plan_key if donation else plan_id,
           "tier": "donation" if donation else "pro", "email": email,
           "status": "created", "at": now_iso(),
           "payUrl": pay["payment_url"],
           "amount": amount, "currency": "USD"}
    if donation:
        rec["donation"] = True
    with _codes_lock:
        data = _load_codes()
        data["payments"].append(rec)
        _save_codes(data)
    _event("payment_created", ref, f"lava · {rec['plan']} · ${amount} · {email}")
    return JSONResponse({"ok": True, "paymentUrl": pay["payment_url"]},
                        headers={**_NOCACHE, **_CORS})


def _success_page(pid: str) -> str:
    """HTML success-страницы (неоновый стиль лендинга): код + license key или «обрабатывается»."""
    import html as _html
    pid = pid[:64]
    p = None
    if pid:
        data = _load_codes()
        # security: матчим ТОЛЬКО наш ref (r-<16 hex>, 64 бита). platega_id убран из
        # сравнения: внешние ID могут быть предсказуемы.
        p = next((x for x in reversed(data["payments"])
                  if pid in (x.get("id"), x.get("ref"))), None)
    paid = bool(p and p.get("status") in _PAID_STATUSES and p.get("code"))
    esc = _html.escape
    if paid and p.get("donation"):
        # донат — без кода и ключа, просто спасибо
        body = """
<h1>💛 Спасибо за поддержку</h1>
<p class="dim">Платёж дошёл. Это чистая поддержка проекта — без ключа и подписки, просто спасибо.
Такие переводы платят за серверы и время на разработку.<br>
Если это была ошибка — напиши на help@rezon-ai.ru, вернём.</p>"""
        refresh = ""
    elif paid:
        code = esc(str(p["code"]))
        lic = esc(str(p.get("licenseKey") or ""))
        tier = esc(str(p.get("tier") or ""))
        body = f"""
<h1>✨ Оплата прошла</h1>
<p class="dim">Maxgraph {tier} — сохрани оба значения, они больше нигде не показываются разом:</p>
<div class="lbl">Резервный код</div><div class="code">{code}</div>
<div class="lbl">Ключ активации</div><div class="code small">{lic}</div>
<p class="dim">Ключ → дашборд MaxGraph → статус лицензии вверху → «Активировать»
(при покупке из дашборда ключ активируется сам).<br>
Резервный код: {code} — сохрани: он нужен для восстановления ключа через поддержку (help@rezon-ai.ru).<br>
Письмо с этими же данными отправлено на email (если SMTP настроен).</p>"""
        refresh = ""
    else:
        body = """
<h1>⏳ Платёж обрабатывается</h1>
<p class="dim">Platega ещё не подтвердила оплату — обнови страницу через несколько секунд.
Ключ активации появится здесь автоматически.</p>"""
        refresh = "<script>setTimeout(()=>location.reload(),5000)</script>"
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<link rel="icon" href="data:,"><title>Maxgraph — оплата</title>
<style>
/* DESIGN-SYSTEM-v3 «Beehiiv Dark» — плоские поверхности, без неона */
body{{background:#060419;color:#f7f5ff;font:16px/1.5 "Segoe UI",system-ui,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;margin:0}}
.card{{background:#0d0b28;border:1px solid #ffffff1f;border-radius:12px;padding:40px;max-width:640px;width:92%}}
h1{{font-size:28px;font-weight:700;letter-spacing:-.02em;color:#fff;margin:0 0 14px}}
.dim{{color:#c4c2d6;font-size:14px}}
.lbl{{color:#c4c2d6;font-size:12px;font-weight:500;text-transform:uppercase;letter-spacing:.06em;margin:18px 0 6px}}
.code{{font:700 20px Consolas,monospace;letter-spacing:2px;color:#ff5ec4;background:#060419;border:1px solid #ffffff1f;border-radius:8px;padding:12px 16px;word-break:break-all;user-select:all}}
.code.small{{font-size:13px;letter-spacing:.5px;color:#bcbef3}}
a{{color:#7079d5}}
code{{background:#141230;border-radius:4px;padding:1px 6px;font-size:12px}}
</style></head><body><div class="card">{body}
<p class="dim" style="margin-top:22px"><a href="/">← на главную</a></p>
</div>{refresh}</body></html>"""


@mcp.custom_route("/billing/success", methods=["GET"])
async def billing_success(request: Request):
    """Страница после оплаты: код + license key, либо «обрабатывается» (автообновление)."""
    g = _store_gate()
    if g:
        return g
    return HTMLResponse(_success_page(request.query_params.get("pid", "")), headers=_NOCACHE)


@mcp.custom_route("/billing/webhook/platega", methods=["POST"])
async def billing_webhook_platega(request: Request):
    """Platega callback: headers X-MerchantId/X-Secret, тело id/amount/currency/status/paymentMethod/payload.
    Оплата (CONFIRMED) → выдача кода. Без HMAC-подписи у Platega — проверяем credentials-заголовки."""
    import hmac as _hmac
    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if not (PLATEGA_MERCHANT_ID and PLATEGA_API_KEY):
        log.error("platega webhook hit, но ключи не заданы — отклонено")
        return JSONResponse({"error": "webhook not configured"}, status_code=503)
    mid = request.headers.get("x-merchantid", "")
    sec = request.headers.get("x-secret", "")
    if not (_hmac.compare_digest(mid, PLATEGA_MERCHANT_ID) and _hmac.compare_digest(sec, PLATEGA_API_KEY)):
        log.warning("platega webhook: неверные credentials от %s", ip)
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if _rate_limited(ip, "webhook", 30, 60):
        return JSONResponse({"error": "too_many_requests"}, status_code=429)
    _ref_sweep_pending()  # N7: возврат стеков брошенных платежей не ждёт открытия реф-карточки
    raw, err = await _read_capped(request, 65536)
    if err is not None:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        body = json.loads(raw)
    except Exception:
        body = dict(await request.form()) if request.method == "POST" else {}
    if not isinstance(body, dict):  # JSON-массив и т.п. → пусто
        body = {}
    delivery = None  # (code, lic_key, tier, email) — доставим после снятия лока
    donation_delivery = None  # (email, label) — thank-you письмо донатеру
    ref_msgs = []  # TG владельцу о реф-событиях — отправка строго после локов
    with _codes_lock:
        data = _load_codes()
        pid = str(body.get("id") or body.get("payment_id") or body.get("order_id") or "")[:128]
        ref = str(body.get("payload") or "")[:64]
        if not pid:
            import hashlib
            pid = "h-" + hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
        status = str(body.get("status", "")).lower()[:32]
        # существующая запись платежа (создана /api/billing/pay) — по platega id или ref
        payment = next((p for p in data["payments"]
                        if p.get("platega_id") == pid or (ref and p.get("ref") == ref)), None)
        if payment and payment.get("code"):
            return JSONResponse({"ok": True, "dedup": True})
        if payment is None:
            if any(p.get("id") == pid for p in data["payments"]):
                return JSONResponse({"ok": True, "dedup": True})
            payment = {"id": pid, "raw": body, "at": now_iso(), "status": status}
            data["payments"].append(payment)
        payment["status"] = status
        payment["platega_id"] = payment.get("platega_id") or pid
        # H1: терминальный фейл платежа со стеком → стек возвращаем сразу,
        # не дожидаясь свипа 24ч (см. _ref_sweep_pending)
        if status in _FAILED_STATUSES and payment.get("stack_code"):
            with _ref_lock:
                refs = _load_referrals()
                if refs is not None and _ref_restore_locked(refs, payment, "stack_restored_failed"):
                    _save_referrals(refs)
        result = {"ok": True, "recorded": True}
        if status in _PAID_STATUSES:
            try:
                amount = float(body.get("amount") or body.get("sum") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            currency = str(body.get("currency") or body.get("currency_code") or "")
            plan_id = _plan_key(str(payment.get("plan") or "")) or _plan_key(str(payment.get("tier") or ""))
            if not plan_id:
                # записи платежа нет (трим 500) — сумма со скидкой угадала бы младший план; алертим
                plan_id = _plan_for(amount, currency)
                _event("plan_guessed_from_amount", pid, f"{amount}{currency} → {plan_id}")
            exp_amt = payment.get("amount")
            if exp_amt and amount and abs(amount - float(exp_amt)) > 1:
                _event("payment_amount_mismatch", pid,
                       f"{plan_id} · оплачено {amount}{currency} · выставлено {exp_amt}")
            if PLANS[plan_id].get("donation"):
                # донат — чистая поддержка: без кода/ключа/лицензии.
                # code="DONATION" — маркер обработанности для дедупа повторных callback.
                payment["plan"] = plan_id
                payment["tier"] = "donation"
                payment["donation"] = True
                payment["code"] = "DONATION"
                email = (payment.get("email") or _valid_email(body.get("email"))) or None
                if email:
                    payment["email"] = email
                donation_delivery = (email, PLANS[plan_id]["label"])
                _event("donation", pid, f"{PLANS[plan_id]['label']} · {amount}{currency} · {email or 'без почты'}")
                result["donation"] = True
            else:
                days = PLANS[plan_id]["days"]
                # продление — дни плана поверх max(старый exp, сейчас), остаток не сгорает
                ext = str(payment.get("extend") or "")[:600].strip()
                ext_ok = False
                if ext:
                    p2, err2 = licensing.verify_key(LICENSE_SECRET, ext)
                    ext_ok = not err2 and not _is_revoked(data, ext)
                    if ext_ok:
                        old_exp = int(p2.get("exp") or 0)
                        if old_exp > time.time():
                            days += (old_exp - time.time()) / 86400
                code = _new_code()
                existing = {c["code"] for c in data["codes"]}
                while code in existing:
                    code = _new_code()
                data["codes"].append(_code_rec(code, "pro", "sold", f"platega:{pid}"))
                payment["code"] = code
                payment["plan"] = plan_id
                payment["tier"] = "pro"
                # license key: tier всегда pro, срок = длительность плана со дня оплаты
                email = (payment.get("email") or _valid_email(body.get("email"))) or None
                lic_key = licensing.generate_key(LICENSE_SECRET, email or "buyer@unknown", "pro", days)
                payment["licenseKey"] = lic_key
                if ext_ok:
                    # продление = ротация (как recovery): старый ключ гасим с grace 72ч,
                    # иначе один ключ мультиплицирует дни повторными покупками
                    _add_revoked(data, ext, "superseded: продление")
                if email:
                    payment["email"] = email
                # легаси: платёж, привязанный к тенанту (покупка из кабинета, кабинет
                # отменён — для новых платежей tenant не выставляется) → код
                # сразу активирован, license key закреплён за тенантом
                tenant_id = payment.get("tenant")
                if tenant_id:
                    t = next((x for x in data["tenants"] if x.get("id") == tenant_id), None)
                    if t:
                        rec = data["codes"][-1]
                        rec["status"] = "activated"
                        rec["activatedAt"] = now_iso()
                        rec["tenant"] = tenant_id
                        t["tier"] = "pro"
                        t["licenseKey"] = lic_key
                        t["paidAt"] = now_iso()
                        _event("cabinet_paid", tenant_id, f"{plan_id} · {code}")  # имя события — история, не трогаем
                delivery = (code, lic_key, "pro", email, payment.get("ref") or pid)
                _event("payment", pid, f"{plan_id} · {amount}{currency} · код {code}")
                result["code"] = code
                # --- рефералка — начисление
                # приглашающему / self-ref блок / сгорание стека при продлении.
                # refs под своим локом; порядок локов _codes_lock → _ref_lock (везде одинаково).
                ref_code = str(payment.get("ref_code") or "")
                b_iid = str(payment.get("buyer_install") or "")
                b_fp = str(payment.get("buyer_fp") or "")
                if ref_code or (ext_ok and b_iid):
                    with _ref_lock:
                        refs = _load_referrals()
                        if refs is None:
                            log.error("referrals.json повреждён — реф-начисление платежа %s пропущено", pid)
                        else:
                            dirty = False
                            if ref_code:
                                rrec = refs.get(ref_code)
                                if rrec is None:
                                    log.error("ref_code %s платежа %s отсутствует в referrals.json", ref_code, pid)
                                else:
                                    hist = {"ts": now_iso(), "payment_id": payment.get("ref") or pid,
                                            "amount": amount}
                                    # shortcut: self-ref только по install_id/fp платежа
                                    # (email покупателя может отсутствовать — см. /api/billing/pay)
                                    if (b_iid and b_iid == rrec.get("install_id")) or \
                                       (b_fp and rrec.get("fp") and b_fp == rrec.get("fp")):
                                        payment["selfref"] = True
                                        hist["flagged"] = "self_ref"
                                        rrec.setdefault("history", []).append(hist)
                                        rrec["history"] = rrec["history"][-200:]
                                        ref_msgs.append(f"⚠️ Self-ref блок: код {ref_code} · "
                                                        f"{amount:.0f} {currency} · начисление отклонено")
                                        _event("ref_self_blocked", ref_code, f"{amount:.0f}{currency}")
                                    else:
                                        rrec["buys"] = _safe_int(rrec.get("buys")) + 1
                                        rrec["stack_n"] = min(_safe_int(rrec.get("stack_n")) + 1,
                                                              REF_STACK_CAP)
                                        rrec.setdefault("history", []).append(hist)
                                        rrec["history"] = rrec["history"][-200:]
                                        ref_msgs.append(f"🤝 Реф-продажа по {ref_code}: "
                                                        f"{amount:.0f} {currency} · всего {rrec['buys']}")
                                        _event("ref_sale", ref_code, f"{amount:.0f}{currency} · buys={rrec['buys']}")
                                    dirty = True
                            # H1: стек сгорает при СОЗДАНИИ платежа. Если платёж
                            # провисел >24ч, свип вернул стек (stack_restored), а оплата
                            # всё же дошла — сжигаем повторно (иначе двойная выгода).
                            if ext_ok and payment.get("stack_code") and payment.get("stack_restored"):
                                own = refs.get(str(payment.get("stack_code") or ""))
                                spent = max(0, (_safe_int(payment.get("extend_pct")) - 10) // 10)
                                if own is not None and spent:
                                    own["stack_n"] = max(0, _safe_int(own.get("stack_n")) - spent)
                                    own.setdefault("history", []).append(
                                        {"ts": now_iso(), "payment_id": payment.get("ref") or pid,
                                         "amount": payment.get("amount"),
                                         "flagged": "stack_reburn_late_paid"})
                                    own["history"] = own["history"][-200:]
                                    dirty = True
                                    _event("ref_stack_reburn", str(payment["stack_code"]),
                                           f"поздняя оплата {payment.get('ref') or pid}")
                                payment["stack_restored"] = False  # деньги пришли — возврату не бывать
                            if dirty:
                                _save_referrals(refs)
        _save_codes(data)
    if donation_delivery:  # thank-you донатеру — сеть строго вне лока, best-effort
        import asyncio
        d_email, d_label = donation_delivery
        try:
            res = await asyncio.to_thread(billing.deliver_donation, label=d_label, email=d_email)
            log.info("deliver_donation: %s", res)
        except Exception as e:  # deliver_donation сам не бросает; страховка
            log.error("deliver_donation упал целиком (%s)", e)
    if delivery:  # сеть — строго после _save_codes и вне лока; best-effort по каналам
        import asyncio
        code, lic_key, tier, email, success_pid = delivery
        try:
            res = await asyncio.to_thread(billing.deliver_code, code=code, license_key=lic_key,
                                          tier=tier, email=email,
                                          success_url=f"{PUBLIC_BASE_URL}/billing/success?pid={success_pid}")
            log.info("deliver_code %s: %s", code, res)
        except Exception as e:  # deliver_code сам не бросает; страховка
            log.error("deliver_code упал целиком (%s): код %s уже в codes.json и на success-странице", e, code)
    if ref_msgs:  # TG владельцу о реф-продаже/self-ref — после локов, best-effort
        import asyncio
        tg_token = os.environ.get("TG_BOT_TOKEN", "")
        if TG_NOTIFY_CHAT_ID and tg_token:
            for msg in ref_msgs:
                try:
                    await asyncio.to_thread(billing.send_telegram, TG_NOTIFY_CHAT_ID, msg, tg_token)
                except Exception as e:
                    log.error("ref telegram → %s", e)
    return JSONResponse(result, headers=_NOCACHE)


@mcp.custom_route("/api/webhooks/lava", methods=["POST"])
async def webhook_lava(request: Request):
    """lava.top webhook: auth — заголовок X-Api-Key с нашим LAVA_WEBHOOK_KEY
    (задаётся в кабинете lava при регистрации webhook). payment.success → выдача
    кода+license key; refund.success → revoke ключа. Незнакомые события → 200 + лог
    (lava ретраит не-2xx). Дедуп по event_id (payment.* без event_id — synthetic
    contractId:eventType, см. lava.parse_webhook), реестр — codes.json lava_events.
    """
    import hmac as _hmac
    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if not (LAVA_WEBHOOK_KEY and lava):
        log.error("lava webhook hit, но LAVA_WEBHOOK_KEY не задан — отклонено")
        return JSONResponse({"error": "webhook not configured"}, status_code=503)
    if not _hmac.compare_digest(request.headers.get("x-api-key", ""), LAVA_WEBHOOK_KEY):
        log.warning("lava webhook: неверный X-Api-Key от %s", ip)
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if _rate_limited(ip, "lava_hook", 30, 60):
        return JSONResponse({"error": "too_many_requests"}, status_code=429)
    raw, err = await _read_capped(request, 65536)
    if err is not None:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        body = json.loads(raw)
    except Exception:
        body = {}
    ev = lava.parse_webhook(body)
    etype = ev["event_type"]
    delivery = None  # (code, lic_key, tier, email, ref) — доставим после снятия лока
    donation_delivery = None  # (email, label) — thank-you письмо донатеру
    with _codes_lock:
        data = _load_codes()
        seen = data.setdefault("lava_events", [])
        if ev["event_id"] and ev["event_id"] in seen:
            return JSONResponse({"ok": True, "dedup": True})
        result = {"ok": True}
        if etype == "payment.success":
            payment = next((p for p in data["payments"]
                            if p.get("lava_id") == ev["contract_id"]), None)
            if payment and payment.get("code"):
                return JSONResponse({"ok": True, "dedup": True})
            if ev["product_id"] and ev["product_id"] != lava.PRODUCT_ID:
                log.warning("lava payment.success: чужой продукт %s — пропуск", ev["product_id"])
                _event("lava_foreign_product", ev["contract_id"], ev["product_id"])
            elif payment is not None and payment.get("donation"):
                # донат — чистая поддержка: без кода/ключа/лицензии (зеркало Platega-ветки).
                # code="DONATION" — маркер обработанности для дедупа повторных webhook.
                payment["status"] = "success"
                payment["code"] = "DONATION"
                payment["amount"] = ev["amount"] or payment.get("amount")
                payment["currency"] = ev["currency"] or "USD"
                email = payment.get("email") or _valid_email(ev["email"]) or None
                if email:
                    payment["email"] = email
                label = f"MaxGraph — поддержка проекта (${payment.get('amount') or ''})"
                donation_delivery = (email, label)
                _event("donation", ev["contract_id"],
                       f"lava · {payment.get('plan')} · {payment['amount']}{payment['currency']} · {email or 'без почты'}")
                result["donation"] = True
            else:
                plan_id = str((payment or {}).get("plan") or "")
                if plan_id not in PLANS:
                    # fallback по сумме — только USD (оффер USD-dynamic; иная валюта не мапится)
                    if ev["currency"] in ("", "USD"):
                        plan_id = _LAVA_PLAN_BY_AMOUNT.get(round(ev["amount"], 2), "")
                    _event("plan_guessed_from_amount", ev["contract_id"],
                           f"{ev['amount']}{ev['currency']} → {plan_id or '?'}")
                if not plan_id:
                    log.error("lava payment.success: сумма %s %s не мапится на план — ключ НЕ выдан",
                              ev["amount"], ev["currency"])
                    _event("lava_unknown_amount", ev["contract_id"],
                           f"{ev['amount']}{ev['currency']}")
                elif payment is not None and payment.get("amount") \
                        and ev["amount"] and ev["amount"] < float(payment["amount"]) - 0.01:
                    # недоплата против выставленного инвойса — ключ НЕ выдаём
                    log.error("lava payment.success: недоплата по %s: оплачено %s, выставлено %s",
                              ev["contract_id"], ev["amount"], payment["amount"])
                    _event("payment_amount_mismatch", ev["contract_id"],
                           f"{plan_id} · оплачено {ev['amount']}{ev['currency']} · выставлено {payment['amount']}")
                else:
                    if payment is not None and payment.get("amount") \
                            and abs(ev["amount"] - float(payment["amount"])) > 0.01:
                        _event("payment_amount_mismatch", ev["contract_id"],
                               f"{plan_id} · оплачено {ev['amount']}{ev['currency']} · выставлено {payment['amount']}")
                    days = PLANS[plan_id]["days"]
                    code = _new_code()
                    existing = {c["code"] for c in data["codes"]}
                    while code in existing:
                        code = _new_code()
                    data["codes"].append(_code_rec(code, "pro", "sold", f"lava:{ev['contract_id']}"))
                    email = (payment or {}).get("email") or _valid_email(ev["email"]) or None
                    lic_key = licensing.generate_key(LICENSE_SECRET, email or "buyer@unknown", "pro", days)
                    if payment is None:
                        # запись вытеснена тримом: ref — НАШ случайный (как у /billing/create),
                        # contractId (внешний ID lava) — только в lava_id, иначе знание
                        # contractId открывало бы success-страницу с ключом (MED-1)
                        import secrets as _secrets
                        ref = "r-" + _secrets.token_hex(8)
                        payment = {"id": ref, "ref": ref,
                                   "lava_id": ev["contract_id"], "at": now_iso()}
                        data["payments"].append(payment)
                    payment.update({"status": "success", "code": code, "plan": plan_id,
                                    "tier": "pro", "licenseKey": lic_key,
                                    "amount": ev["amount"], "currency": ev["currency"] or "USD"})
                    if email:
                        payment["email"] = email
                    delivery = (code, lic_key, "pro", email, payment.get("ref") or ev["contract_id"])
                    _event("payment", ev["contract_id"],
                           f"lava · {plan_id} · {ev['amount']}{ev['currency']} · код {code}")
        elif etype in ("refund.success", "chargeback.initiated"):
            # возврат и чарджбэк — одна ветка: продажа refunded, ключ в revoke-реестр.
            # чужой продукт (тот же аккаунт lava) не трогает наши ключи (MED-2)
            if ev["product_id"] and ev["product_id"] != lava.PRODUCT_ID:
                log.warning("lava %s: чужой продукт %s — пропуск", etype, ev["product_id"])
                _event("lava_foreign_product", ev["event_id"], f"{etype} · {ev['product_id']}")
            else:
                hit = None
                if ev["contract_id"]:
                    # точный матч по contractId инвойса (security MED-2: эвристика
                    # email+amount при коллизии $5 доната == pro1 могла отозвать
                    # чужой ключ). contractId есть — эвристику не применяем.
                    hit = next((p for p in reversed(data["payments"])
                                if p.get("lava_id") == ev["contract_id"]
                                and p.get("status") != "refunded"), None)
                    if hit and hit.get("donation") and hit.get("status") != "success":
                        hit = None  # неоплаченный донат — журналировать нечего (LOW-1)
                else:
                    for p in reversed(data["payments"]):
                        # донаты матчим тоже (без licenseKey) — журнал возврата, revoke не нужен
                        if not p.get("lava_id") or not (p.get("licenseKey") or p.get("donation")):
                            continue
                        if p.get("status") == "refunded":
                            continue
                        if p.get("donation") and p.get("status") != "success":
                            continue  # неоплаченный донат (LOW-1)
                        if ev["email"] and p.get("email") and p["email"] != ev["email"]:
                            continue
                        if ev["amount"] and p.get("amount") and abs(float(p["amount"]) - ev["amount"]) > 0.01:
                            continue
                        hit = p
                        break
                if hit:
                    hit["status"] = "refunded"
                    hit["refunded_at"] = now_iso()
                    if hit.get("licenseKey"):
                        _add_revoked(data, hit["licenseKey"], f"lava {etype}")
                        note = "ключ отозван"
                    else:  # донат: ключа нет — только журнал
                        note = "донат, ключа не было"
                    _event("payment_refunded", hit.get("lava_id") or hit.get("id"),
                           f"{ev['amount']}{ev['currency']} · {hit.get('email', '')} · {note}")
                    result["refunded"] = True
                else:
                    log.warning("lava %s: платёж не найден (email=%s amount=%s %s)",
                                etype, ev["email"], ev["amount"], ev["currency"])
                    _event("lava_refund_unmatched", "", f"{etype} · {ev['email']} · {ev['amount']}{ev['currency']}")
        elif etype:
            log.info("lava webhook: событие %s (%s) — записано без обработки",
                     etype, ev["contract_id"] or ev["event_id"])
            _event("lava_event", ev["contract_id"] or ev["event_id"], etype)
        if ev["event_id"]:
            seen.append(ev["event_id"])
            data["lava_events"] = seen[-500:]  # потолок, как у payments/revoked
        _save_codes(data)
    if delivery:  # сеть — строго после _save_codes и вне лока; best-effort по каналам
        import asyncio
        code, lic_key, tier, email, success_pid = delivery
        try:
            res = await asyncio.to_thread(billing.deliver_code, code=code, license_key=lic_key,
                                          tier=tier, email=email, lang="en",
                                          success_url=f"{PUBLIC_BASE_URL}/billing/success?pid={success_pid}")
            log.info("lava deliver_code %s: %s", code, res)
        except Exception as e:  # deliver_code сам не бросает; страховка
            log.error("deliver_code упал целиком (%s): код %s уже в codes.json и на success-странице", e, code)
    if donation_delivery:  # thank-you донатеру — сеть строго вне лока, best-effort
        import asyncio
        d_email, d_label = donation_delivery
        try:
            res = await asyncio.to_thread(billing.deliver_donation, label=d_label, email=d_email)
            log.info("lava deliver_donation: %s", res)
        except Exception as e:  # deliver_donation сам не бросает; страховка
            log.error("deliver_donation упал целиком (%s)", e)
    return JSONResponse(result, headers=_NOCACHE)


# --- digiseller per-order ---

async def _digiseller_params(request: Request) -> dict:
    """Плоский dict[str,str] из query + тела (form или JSON) — Digiseller шлёт POST, принимаем и GET."""
    params = {k: str(v) for k, v in request.query_params.items()}
    if request.method == "POST":
        ct = request.headers.get("content-type", "")
        try:
            if "json" in ct:
                body = await request.json()
                if isinstance(body, dict):
                    params.update({k: str(v) for k, v in body.items()
                                   if not isinstance(v, (dict, list))})
            else:
                form = await request.form()
                params.update({k: str(v) for k, v in form.items()})
        except Exception as e:
            log.warning("digiseller gen: не смог разобрать тело от %s: %s", _client_ip(request), e)
    return params


def _dg_response(*, code=None, error=None, fmt="text", status=200, product_id="", inv=0):
    body, ctype = digiseller.build_response(code=code, error=error, fmt=fmt,
                                            product_id=product_id, inv=inv)
    return Response(body, status_code=status, media_type=ctype)


@mcp.custom_route("/billing/digiseller/gen", methods=["GET", "POST"])
async def billing_digiseller_gen(request: Request):
    """Digiseller «данные, полученные от вашего сервера»: свежий код по номеру заказа.

    Контракт и настройка кабинета задокументированы вне репо. Идемпотентно по order id
    (Digiseller дёргает повторно). Код пишется в codes.json ДО ответа; доставка
    письмом — best-effort после снятия лока. Бизнес-отказы — HTTP 200 + поле error
    (по контракту HTTP-ошибка/ответ без goods/error = до 50 ретраев).
    """
    g = _store_gate()
    if g:
        return g
    fmt = DIGISELLER_RESPONSE_FORMAT
    ip = _client_ip(request)
    if _rate_limited(ip, "digiseller_gen", 30, 60):
        return _dg_response(error="too_many_requests", fmt=fmt, status=429)
    # кап по факту длины (content-length может врать); тело ниже разбирает _digiseller_params
    _, err = await _read_capped(request, 16384)
    if err is not None:
        return _dg_response(error="payload too large", fmt=fmt, status=413)
    params = await _digiseller_params(request)
    log.info("digiseller gen вход от %s: поля=%s", ip,
             sorted(k for k in params if k not in ("key", "token", "sign", "sign2")))
    dg_id, dg_inv = params.get("id", ""), params.get("inv", 0)
    if not DIGISELLER_GEN_SECRET:
        log.error("digiseller gen hit, но DIGISELLER_GEN_SECRET не задан — отклонено")
        return _dg_response(error="not configured", fmt=fmt, product_id=dg_id, inv=dg_inv)
    if not digiseller.verify_signature(params, DIGISELLER_GEN_SECRET, DIGISELLER_SIGN_SCHEME):
        log.warning("digiseller gen: неверная подпись от %s (поля: %s)", ip, sorted(params.keys()))
        return _dg_response(error="forbidden", fmt=fmt, product_id=dg_id, inv=dg_inv)
    delivery = None  # (code, lic_key, tier, email) — доставим после снятия лока
    with _codes_lock:
        data = _load_codes()
        try:
            res = digiseller.handle_order(
                data, params,
                new_code=_new_code, code_rec=_code_rec,
                gen_license_key=lambda email, tier, days: licensing.generate_key(
                    LICENSE_SECRET, email, tier, days),
                plan_for_amount=_plan_for, now_iso=now_iso,
                days_for_plan=lambda p: PLANS[p]["days"], sub_days=LICENSE_SUB_DAYS,
                default_plan="pro1",
                expected_product_id=DIGISELLER_PRODUCT_ID)
        except digiseller.DigisellerError as e:
            log.warning("digiseller gen отклонён от %s: %s", ip, e)
            return _dg_response(error=str(e), fmt=fmt, product_id=dg_id, inv=dg_inv)
        _save_codes(data)
        if not res["dedup"]:
            delivery = (res["code"], res["licenseKey"], res["tier"], res.get("email"))
    _event("digiseller_gen", res["payId"],
           f'{res["tier"]} · {res["code"]}' + (" · dedup" if res["dedup"] else ""))
    log.info("digiseller gen: заказ %s → %s (%s)%s", res["order"], res["code"], res["tier"],
             " · dedup" if res["dedup"] else "")
    if delivery:  # сеть — строго после _save_codes и вне лока; best-effort по каналам
        import asyncio
        code, lic_key, tier, email = delivery
        try:
            dres = await asyncio.to_thread(billing.deliver_code, code=code, license_key=lic_key,
                                           tier=tier, email=email,
                                           success_url=f"{PUBLIC_BASE_URL}/billing/success?pid={urllib.parse.quote(res['payId'], safe='')}")
            log.info("digiseller deliver_code %s: %s", code, dres)
        except Exception as e:  # deliver_code сам не бросает; страховка
            log.error("deliver_code упал целиком (%s): код %s уже в codes.json", e, code)
    # Эхо id/inv — устойчиво к нестандартным именам полей: если Digiseller прислал
    # заказ/товар под другим ключом (id_cart, id_d…), отвечаем разрешёнными значениями,
    # иначе их движок не сопоставляет ответ заказу и товар не выдаётся.
    ok_id = params.get("id") or digiseller.product_id_of(params) or str(DIGISELLER_PRODUCT_ID or "")
    ok_inv = params.get("inv") or res["order"]
    # товар = код + пошаговая инструкция (голый код терял покупателя)
    return _dg_response(code=digiseller.goods_text(res["code"]), fmt=fmt, product_id=ok_id, inv=ok_inv)


# --- digiseller уведомления: sale/message → webhooks.jsonl + Telegram ---

def _hook_authorized(request: Request) -> bool:
    import hmac as _h
    key = str(request.query_params.get("key") or "")[:128]
    return bool(DIGISELLER_HOOK_SECRET) and _h.compare_digest(key, DIGISELLER_HOOK_SECRET)


async def _digiseller_hook(request: Request, kind: str):
    """Общий приёмник уведомлений Digiseller (GET/POST). Лог всегда, TG — best-effort."""
    import asyncio
    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if _rate_limited(ip, "dg_hook", 30, 60):
        return JSONResponse({"ok": False, "error": "too_many_requests"}, status_code=429)
    if not _hook_authorized(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    params = await _digiseller_params(request)
    params.pop("key", None)
    d = webhooks.normalize(params)
    try:
        webhooks.append_log(WEBHOOKS_LOG, kind, d, params)
        try:
            os.chmod(WEBHOOKS_LOG, 0o600)  # email/заказы покупателей — только owner
        except OSError:
            pass
    except Exception as e:
        log.error("webhooks log упал: %s", e)
    _event(f"dg_notify_{kind}", d.get("order") or d.get("debate_id"), d.get("email") or d.get("message", "")[:80])
    tg_token = os.environ.get("TG_BOT_TOKEN", "")
    if TG_NOTIFY_CHAT_ID and tg_token:
        text = webhooks.format_telegram(kind, d)
        try:
            await asyncio.to_thread(billing.send_telegram, TG_NOTIFY_CHAT_ID, text, tg_token)
        except Exception as e:
            log.error("dg hook telegram → %s", e)
    return JSONResponse({"ok": True}, headers=_NOCACHE)


@mcp.custom_route("/webhook/digiseller/sale", methods=["GET", "POST"])
async def digiseller_hook_sale(request: Request):
    return await _digiseller_hook(request, "sale")


@mcp.custom_route("/api/hooks/github", methods=["POST"])
async def github_hook(request: Request):
    """GitHub repository webhook (витрина RuMemory/maxgraph) → TG владельцу.

    Только магазин (STORE_MODE); подпись X-Hub-Signature-256 по GITHUB_HOOK_SECRET;
    неизвестные события → 200 delivered=false, не спамим TG."""
    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if _rate_limited(ip, "gh_hook", 30, 60):
        return JSONResponse({"ok": False, "error": "too_many_requests"}, status_code=429)
    body, err = await _read_capped(request, 262144)
    if err is not None:
        return err
    secret = os.environ.get("GITHUB_HOOK_SECRET", "")
    if not secret:  # не настроено — неотличимо от 404 роутера
        return PlainTextResponse("Not Found", status_code=404)
    if not ghhook.verify_signature(secret, body, request.headers.get("x-hub-signature-256", "")):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    event = request.headers.get("x-github-event", "")
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    text = ghhook.format_telegram(event, payload)
    _event("github_hook", event or None, (text or "ignored")[:80])
    tg_token = os.environ.get("TG_BOT_TOKEN", "")
    if text and TG_NOTIFY_CHAT_ID and tg_token:
        import asyncio
        try:
            await asyncio.to_thread(billing.send_telegram, TG_NOTIFY_CHAT_ID, text, tg_token)
        except Exception as e:  # TG down — вебхук всё равно 200, GitHub не ретраит вечно
            log.error("gh hook telegram: %s", e)
    return JSONResponse({"ok": True, "delivered": bool(text)}, headers=_NOCACHE)


@mcp.custom_route("/webhook/digiseller/message", methods=["GET", "POST"])
async def digiseller_hook_message(request: Request):
    return await _digiseller_hook(request, "message")


# --- telegram deep-link доставка: t.me/Maxgraph_bot?start=<ref> → код в личку ---

def _tg_on_start(chat_id: str, payload: str) -> str:
    """Deep-link /start <ref> → код+license key платежа в личку.

    ref (r-<16 hex>, 64 бита) раньше показывался на success-странице (ссылка
    убрана — бот не работает с покупателями); обработчик оставлен
    для старых ссылок: знание ref = доказательство владения платежом.
    Вызывается из потока tgbot — только чтение codes.json, без сети
    (sendMessage делает сам поллер).
    """
    ref = (payload or "")[:64].strip()
    if not ref:
        return "👋 Это бот Maxgraph."
    data = _load_codes()
    p = next((x for x in data["payments"] if ref in (x.get("id"), x.get("ref"))), None)
    if not p or p.get("status") not in _PAID_STATUSES or not p.get("code"):
        return "⏳ Платёж ещё обрабатывается — вернись по той же ссылке через минуту."
    if p.get("donation"):
        return "💛 Спасибо за поддержку MaxGraph! Это была чистая поддержка — без ключа и подписки."
    return billing.delivery_text(p["code"], p.get("licenseKey") or "", p.get("tier") or "pro")


# --- embed: фоновый пополнитель векторов для гибридного поиска ---

def embed_loop(cfg: dict):
    """Фоновый цикл пополнителя embeddings (daemon-поток).
    Fail-open: любой сбой тика логируется, поток живёт; сеть — fail-open None
    внутри embed (embed_texts). Пульс НЕ пишется (фоновый тик раз в 5 минут
    засорял бы timeline)."""
    log.info("embed: цикл запущен (refresh=%ss, batch=%s, cap=%s tok/day, model=%s)",
             cfg["refresh_sec"], embed.BATCH_SIZE, cfg["daily_cap"], cfg["model"])
    while True:
        try:
            with _lock:
                ents, _ = _load()
            stats = embed.refresh(list(ents.values()), EMBEDDINGS_FILE,
                                  EMBED_STATE_FILE, cfg)
            if stats.get("embedded"):
                log.info("embed: %s векторов обновлено (%s батчей, ~%s токенов, "
                         "pending=%s)", stats["embedded"], stats["batches"],
                         stats["tokens"], stats["pending"])
        except Exception:
            log.exception("embed: сбой тика — продолжаем (fail-open)")
        time.sleep(cfg["refresh_sec"])


_embed_start_lock = threading.Lock()


def _ensure_embed_loop():
    """Поднять фоновый embed-цикл, если он ещё не стартовал. Идемпотентно;
    READONLY-зеркало не пишет — не стартуем. (С 1.9.10 embed включается
    только env EMBED_URL/EMBED_KEY при boot — вызов оставлен для совместимости.)"""
    global EMBED_ON
    if READONLY or not embed.enabled():
        return
    with _embed_start_lock:
        if any(t.name == "embed" and t.is_alive() for t in threading.enumerate()):
            return
        threading.Thread(target=embed_loop, args=(embed.config_from_env(),),
                         daemon=True, name="embed").start()
        EMBED_ON = True
        log.info("embed: enabled")


# --- actr: периодический flush счётчиков обращений → access.json ---

def _actr_flush_quiet():
    """atexit: финальный flush при graceful shutdown. Никогда не падает:
    исключение в atexit-хуке печатает traceback в stderr на выходе (мусор
    в логах), а сам Store.flush уже fail-open."""
    try:
        _actr.flush(ACCESS_FILE)
    except Exception:
        pass


def actr_loop(cfg: dict):
    """Фоновый flush счётчиков ACT-R → data/access.json (daemon-поток, как
    embed_loop — бесконечный цикл, graceful shutdown через
    atexit). Fail-open: сбой тика логируется, поток живёт. Пульс НЕ пишется
    (фоновый тик раз в 5 минут засорял бы timeline); события flush — нет."""
    log.info("actr: flush-цикл запущен (interval=%ss)", cfg["flush_sec"])
    while True:
        time.sleep(cfg["flush_sec"])
        try:
            _actr.flush(ACCESS_FILE)
        except Exception:
            log.exception("actr: сбой flush — продолжаем (fail-open)")


# ------------------------------------------------------------------ auth ----

class TokenAuth:
    """Pure-ASGI bearer-token middleware. Публичные пути — в PUBLIC_PATHS ниже
    (лендинг, оболочка /app, redeem/trial, биллинг, вебхуки, /healthz).

    Master-токен — ТОЛЬКО точное совпадение `Authorization: Bearer <MEMORY_TOKEN>`:
    аутентификации по query нет — `?token=` удалён: токен не должен
    жить в access-логах uvicorn/nginx.

    /admin — HTML-оболочка только на инстансе магазина (STORE_MODE=1,
    иначе 404); её данные идут через непубличные /api/* под Bearer.
    """

    PUBLIC_PATHS = {"/healthz", "/", "/admin", "/app", "/landing", "/og.png", "/favicon.ico",
                    "/privacy", "/offer",  # правовые страницы лендинга
                    "/.well-known/security.txt",  # RFC 9116: контакт для security-репортов
                    "/api/codes/redeem", "/api/plans",
                    "/api/billing/pay", "/api/billing/status", "/billing/result", "/billing/webhook/platega",
                    "/billing/create", "/billing/success", "/api/license/validate",
                    "/api/billing/lava/invoice", "/api/webhooks/lava",  # lava.top (EN-витрина, USD)
                    "/api/telemetry/install",  # анонимный install-ping
                    "/api/telemetry/heartbeat",  # анонимный heartbeat живости (native-client)
                    "/api/support/report",  # отчёт о баге из дашборда (только магазин, rate-limited)
                    "/api/version",  # апдейт-чип дашборда (публичная версия)
                    "/api/admin/me",  # страница входа админки (configured/authed — не секрет)
                    "/api/admin/login", "/api/admin/setup",  # логин/первичная настройка (rate-limited)
                    "/api/referral/code",  # реф-код инстанса (install_id+fp → code+статы)
                    "/api/releases/latest.tar.gz", "/api/releases/latest.sha256",  # автообновления
                    "/api/releases/latest.sig",  # ed25519-подпись релиза
                    "/billing/digiseller/gen",  # --- digiseller per-order ---
                    "/webhook/digiseller/sale", "/webhook/digiseller/message",  # уведомления
                    "/api/hooks/github"}  # GitHub-вебхук витрины → TG владельцу

    # Префиксные публичные пути: статика лендинга (self-hosted шрифты);
    # native-релизы — клиенты ходят за апдейтами без токена;
    # имя файла дополнительно жёстко валидируется regex в хендлере.
    PUBLIC_PREFIXES = ("/static/", "/api/releases/native/")

    if not STORE_MODE:
        # self-hosted: activate публичен (пользователь активирует свою копию).
        # STORE_MODE=1 (инстанс магазина): публичный activate позволял бы любому
        # с валидным ключом перезаписать license.json магазина → отложенный DoS
        # после exp ключа — поэтому тут его нет.
        PUBLIC_PATHS = PUBLIC_PATHS | {"/api/license/activate"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            # канонический хост self-hosted UI — localhost. Токен дашборда живёт
            # в localStorage и привязан к origin, а Docker Desktop открывает 127.0.0.1 →
            # покупатель видел гейт без токена. Только GET/HEAD: POST /mcp и /api/*
            # с 127.0.0.1 продолжают работать без редиректа.
            if scope.get("method") in ("GET", "HEAD"):
                host = dict(scope.get("headers") or []).get(b"host", b"").decode("utf-8", "ignore")
                # строгий матч: порт только цифрами — иначе userinfo-трюк
                # (127.0.0.1:8787@evil.com) уводил бы Location на чужой домен
                m = re.fullmatch(r"127\.0\.0\.1(?::(\d{1,5}))?", host)
                if m:
                    # latin-1: байты qs 1:1 в символы → заголовок не упадёт на non-ASCII
                    qs = scope.get("query_string", b"").decode("latin-1")
                    loc = ("http://localhost" + (":" + m.group(1) if m.group(1) else "")
                           + urllib.parse.quote(path, safe="/%") + (f"?{qs}" if qs else ""))
                    resp = Response(status_code=308, headers={"location": loc})
                    await resp(scope, receive, send)
                    return
            if path not in self.PUBLIC_PATHS and not path.startswith(self.PUBLIC_PREFIXES):
                headers = dict(scope.get("headers") or [])
                auth = headers.get(b"authorization", b"").decode("utf-8", "ignore")
                ok = bool(TOKEN) and hmac.compare_digest(auth, f"Bearer {TOKEN}")  # без timing-оракула
                if not ok and STORE_MODE and path in _ADMIN_COOKIE_PATHS:
                    # админка магазина — сессионная кука; хендлер сам проверит
                    # _admin_auth() и вернёт 401. Self-hosted: кука не играет.
                    ok = _admin_session_ok_cookie(
                        headers.get(b"cookie", b"").decode("utf-8", "ignore"))
                if not ok:
                    resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                    await resp(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def main():
    _require_store_token()  # магазин без явного MEMORY_TOKEN → fail-fast, не ротация
    _migrate_to_shards()  # одноразовая разрезка memory.json → projects/ (с бэкапом)
    _heal_repo_tag_projects()  # самолечение: данные из repo-тегов назад в имя папки
    st = _license_status()  # при старте: создаёт license.json на первом запуске, ловит fail-closed
    log.info("license: %s (days_left=%s)%s", st.get("state"), st.get("days_left"),
             "" if licensing.allows_write(st) else " — МУТАЦИИ ЗАБЛОКИРОВАНЫ, activate: /#activate")
    if not STORE_MODE and st.get("fp_class") == "unanchored":
        # Ни hostfp.json, ни валидного env HOST_FP: fingerprint контейнерный и плывёт
        # при пересоздании — учёт машин/триала на магазине будет неточным.
        # Лечение без переустановки: повторный запуск установщика досчитает hostfp.json.
        log.warning("установка не привязана к железу хоста (нет hostfp.json/HOST_FP) — "
                    "учёт триала/машин будет неточным; лечение: пере-run install.sh/install.ps1 "
                    "(досчитает hostfp.json), затем recreate контейнера")
    # полный токен — ровно один раз при генерации (онбординг «токен в логах»);
    # при чтении из файла молчим — токен не тонет в журналах при каждом рестарте
    if _TOKEN_SOURCE == "generated":
        log.info("MEMORY_TOKEN сгенерирован и сохранён в data/local_token.txt: %s", TOKEN)
    elif _TOKEN_SOURCE == "ephemeral":
        log.warning("MEMORY_TOKEN: data/ read-only — эфемерный токен процесса: %s", TOKEN)
    if not STORE_MODE:
        threading.Thread(target=_bootstrap_hosted, daemon=True, name="bootstrap-hosted").start()  # ping → relay-pull при boot
        threading.Thread(target=_license_watchdog, daemon=True, name="license-watchdog").start()
        # heartbeat раз в 6ч — всем установкам (docker/python/native), не только exe;
        # flavor внутри: "native" при _FROZEN, иначе autodetect как у install-ping.
        # native stdlib-only при импорте (pystray/PIL — лениво) и входит в Docker-образ (COPY app/).
        import native
        native.start_heartbeat(_post_json, PUBLIC_BASE_URL, DATA_DIR, lambda: VERSION)
    env_key = os.environ.get("LICENSE_KEY", "").strip()  # лицензия из env (self-hosted pro)
    if env_key and not STORE_MODE and st.get("state") != "licensed":  # на магазине self-activation не нужен
        res = _activate_key(env_key)
        log.info("LICENSE_KEY: %s", f"ok ({res.get('tier')} · {res.get('email')})" if res.get("ok")
                                   else f"отклонён: {res.get('error')}")
    migrate_token_hashes()  # одноразово: легаси plaintext-токены в codes.json → sha256
    app = TokenAuth(mcp.streamable_http_app())
    if os.environ.get("TG_BOT_TOKEN"):
        try:
            import tgbot
            tgbot.start_poller(os.environ["TG_BOT_TOKEN"], _tg_on_start)  # daemon-поток, getUpdates
            log.info("tgbot: deep-link доставка активна (/start <ref>)")
        except ImportError:
            log.warning("tgbot: TG_BOT_TOKEN задан, но компонент отсутствует в этой сборке")
    global EMBED_ON
    emb_cfg = embed.config_from_env()  # семантический слой (опционально, офлайн-safe)
    EMBED_ON = embed.should_start(emb_cfg, READONLY)
    if EMBED_ON:  # READONLY-зеркало не пишет → не запускаем
        threading.Thread(target=embed_loop, args=(emb_cfg,), daemon=True,
                         name="embed").start()
        log.info("embed: enabled (model=%s, hybrid_w=%s)", emb_cfg["model"],
                 emb_cfg["hybrid_weight"])
    else:
        log.info("embed: disabled (%s)",
                 "READONLY mirror" if embed.enabled(emb_cfg) else "no EMBED_URL/KEY")
    # ACT-R reinforcement. Буст default ON (ACTR_MAX_BOOST=0 → off).
    # Flush — daemon-поток + atexit (daemon'ы умирают с процессом,
    # graceful shutdown = atexit). READONLY-зеркало не пишет (как embed);
    # буст на зеркале при этом работает по счётчикам, завезённым ресинком.
    actr_cfg = actr.config_from_env()
    if not READONLY:
        threading.Thread(target=actr_loop, args=(actr_cfg,), daemon=True,
                         name="actr").start()
        atexit.register(_actr_flush_quiet)
    log.info("actr: boost %s (max_boost=%s, half_life=%sd, flush=%ss%s)",
             "on" if actr_cfg["max_boost"] > 0 else "off (ACTR_MAX_BOOST=0)",
             actr_cfg["max_boost"], actr_cfg["half_life_days"], actr_cfg["flush_sec"],
             "" if not READONLY else ", flush off (READONLY mirror)")
    log.info("maxgraph %s listening on %s:%s (store: %s, store_mode=%s, trust_proxy=%s, readonly=%s)",
             VERSION, HOST, PORT, MEMORY_FILE, STORE_MODE, TRUST_PROXY, READONLY)
    import uvicorn
    # frozen windowed exe: sys.stdout=None — дефолтный log_config uvicorn падает;
    # логирование уже идёт в data/maxgraph.log (ротация)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", proxy_headers=True,
                **({"log_config": None} if _FROZEN else {}))


if __name__ == "__main__":
    if _FROZEN:
        # onefile: child-env несёт _PYI_* (указатель на temp-каталог parent'а) —
        # сбросить ДО любых spawn (рестарт из трея, helper обновления, ie4uinit),
        # иначе порождённые exe читают модули из каталога, который удаляет
        # умирающий parent (баг 2026-10-25: ModuleNotFoundError: unicodedata).
        # На текущий процесс не влияет: bootloader уже отработал.
        import native
        native._scrub_pyi_env(os.environ)
    # helper-процесс подмены exe (Windows не даёт заменить бегущий файл):
    # `maxgraph.exe --mg-apply-update <new_exe> <parent_pid> <sha256> <sig>` —
    # ждёт смерти родителя, ПЕРЕПРОВЕРЯЕТ sha256+ed25519 (B-1), атомарно
    # подменяет exe, перезапускает, healthcheck, откат.
    if _FROZEN and sys.argv[1:2] == ["--mg-apply-update"]:
        import selfupdate
        sys.exit(selfupdate.apply_update_main(sys.argv[2:]))
    # native exe: привязка к железу (hostfp) + автозапуск ОС + трей; MG_NO_TRAY=1 — headless
    if _FROZEN:
        import native  # stdlib-only при импорте; pystray/PIL — лениво в run_tray
        import singleinstance  # stdlib-only; MG_NO_LOCK=1 отключает guard
        if not singleinstance.acquire(DATA_DIR):
            # Второй процесс на тот же data/ (автозапуск HKCU + ручной старт).
            # Сервер НЕ поднимаем: проигравший иначе уходил в «эфемерный токен»
            # и весь API отвечал 401 (конфиги несут файловый токен). Вместо этого
            # открываем дашборд уже живого экземпляра и выходим.
            log.info("single-instance: экземпляр уже запущен (data=%s) — дашборд живого, выход", DATA_DIR)
            native.open_dashboard(PORT)
            sys.exit(0)
        # Гонка ПЕРВОГО старта: до guard'а (на импорте) лузер мог успеть записать
        # СВОЙ mg_local_* в local_token.txt. Под локом файл авторитетен — перечитываем,
        # иначе в памяти победителя чужой токен и снова 401 (тот самый баг).
        TOKEN = _resolve_token()
        native.ensure_hostfp()
        native.ensure_autostart()
        native.ensure_shortcuts()  # Windows: MaxGraph.lnk на рабочий стол + в меню «Пуск»
        # самолечение ~/.maxgraph (хук + hooks.env из ресурсов exe) — иначе
        # автообновление оставляет клиентам файлы хуков от старой версии
        native.ensure_hook_files(token=TOKEN, port=PORT)
        import selfupdate  # фоновые обновления по подписанному каналу магазина
        selfupdate.start_update_loop(lambda: VERSION, PUBLIC_BASE_URL, DATA_DIR, PORT,
                                     on_update=native.notify_update_available)
        # сторож hooks-плагина DSH: автообновление DSH молча отключает плагин
        # старой версии (peer-пины) — перепинываем, иначе захват памяти умирает
        # тихо (инцидент 2026-10-24). Свой daemon-поток, fail-open.
        native.start_dsh_hook_watchdog()
        if os.environ.get("MG_NO_TRAY") != "1":
            native.run_tray(main)   # hostscan стартует внутри (балуну нужна иконка)
        else:
            native.start_host_scan()   # headless: событие в лог, балуна нет
            main()
    else:
        # python-фолбэк (dev/spike): тот же guard, но headless — без webbrowser,
        # просто лог и exit 0. Магазин (STORE_MODE) и Docker (/.dockerenv) — без
        # guard: там процесс на data/ один по устройству среды, поведение не меняем.
        if not STORE_MODE and not os.path.exists("/.dockerenv"):
            import singleinstance
            if not singleinstance.acquire(DATA_DIR):
                log.info("single-instance: экземпляр уже запущен (data=%s) — выход", DATA_DIR)
                sys.exit(0)
            TOKEN = _resolve_token()  # перечитать под локом (гонка первого старта, см. выше)
            import native  # stdlib-only при импорте; pystray/PIL — лениво в run_tray
            native.ensure_hostfp()  # привязка к железу, как у exe (анти-фарм: иначе unanchored)
        main()
