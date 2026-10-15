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

Проекты (Ф1, docs/MEMORY-SYSTEM-V2-SPEC.md): необязательный параметр `project`
(kebab-slug) во всех MCP-инструментах и REST memory-операциях. Пустой project =
плоский граф — поведение до Ф1, default-проект не навязывается. Изоляция чтения:
project=X видит только X (+ сущность project:<slug>); all_projects=true — явный
кросс-поиск. Сущность project:<slug> (entityType "project") автосоздаётся при
первой записи с этим slug; новые сущности связываются с ней «part of».

Env:
  MEMORY_TOKEN  — bearer-токен (Authorization: Bearer, точное совпадение) для всех
                  эндпоинтов вне публичных — полный список в TokenAuth.PUBLIC_PATHS;
                  аутентификации по query нет (удалена в v0.11).
                  Не задан → автогенерация mg_local_* в <data>/local_token.txt (v0.14)
  Телеметрия (всегда ON, v0.41.0: выключатель TELEMETRY=0 удалён решением владельца):
                  анонимный install-ping (v0.14) — один POST {install_id, version, fp?}
                  на магазин при старте. fp (v0.16.3) — sha256-хеш machine fingerprint;
                  магазин отвечает якорем начала триала → снос data/ не сбрасывает 7 дней.
                  Сюда же learn-events (v0.26.0): анонимный пакет
                  «запрос → открытый заголовок» раз в 5 мин (W2)
  INSTALL_FLAVOR — метка способа установки (docker|rancher|python) в install-ping;
                  ставится установщиком (install.sh/install.ps1), пусто →
                  autodetect по маркеру контейнера /.dockerenv (v0.39.0)
  MEMORY_DIR    — каталог данных (default /data)
  PORT          — порт (default 8787)
  READONLY      — READONLY=1: зеркало (resync.py перезаписывает memory.json из прода) —
                  мутации памяти отклоняются (HTTP 403 {"error":"readonly_mirror"}),
                  чтение/поиск/экспорт/дашборд/healthz работают; биллинг не затрагивается
  ACTR_FLUSH_SEC      — В2.3: период flush счётчиков обращений в data/access.json
                        (default 300; + финальный flush на shutdown через atexit)
  ACTR_MAX_BOOST      — В2.3: потолок ACT-R буста ранга (default 0.25 = +25%;
                        0 — буст выключен, поведение byte-identical)
  ACTR_HALF_LIFE_DAYS — В2.3: полураспад recency-фактора в днях (default 14)
"""

import atexit
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.responses import FileResponse
from starlette.responses import Response  # --- digiseller per-order (v0.7) ---

import licensing   # stdlib-only: триал/лицензии (тестируется отдельно)
import plans       # stdlib-only: тарифы/лейблы планов (общие с магазином)
import searchrank  # --- Ф6 (MEMORY-SYSTEM-v2) --- stdlib-only: ранжированный FTS-lite поиск
import embed       # --- В2.2 (волна 2) --- stdlib-only: опциональные эмбеддинги, гибридный поиск
import actr        # --- В2.3 (волна 2) --- stdlib-only: ACT-R reinforcement (частые факты всплывают)
import learn       # --- W2 (v0.26.0) --- stdlib-only: анонимные learn-events «запрос → открытый заголовок»
import rankcfg     # --- W3 (v0.27.0) --- stdlib-only: версионированный словарь синонимов (relay rank)

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
    import ghhook
except ImportError:
    ghhook = None

def _find_version() -> str:
    # Docker: /app/VERSION (копируется Dockerfile'ом); локальный запуск из app/: ../VERSION
    for p in (Path(__file__).with_name("VERSION"), Path(__file__).resolve().parent.parent / "VERSION"):
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    return "0.0.0-dev"


_TOKEN_SOURCE = ""  # env|file|generated|ephemeral — main() логирует токен один раз при генерации


def _resolve_token(env=None, data_dir=None) -> str:
    """MEMORY_TOKEN: env побеждает; иначе автогенерация mg_local_* с персистом
    в <data>/local_token.txt (онбординг без шага «придумай токен», v0.14).
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
DATA_DIR = Path(os.environ.get("MEMORY_DIR", "/data"))
MEMORY_FILE = DATA_DIR / "memory.json"
PROJECTS_DIR = DATA_DIR / "projects"  # v0.34.0: шарды графа по проектам (суд 20261013-000330)
EVENTS_FILE = DATA_DIR / "events.jsonl"
INSTALLS_FILE = DATA_DIR / "installs.json"  # --- v0.14: install-ping учёт ---
LEARN_EVENTS_FILE = DATA_DIR / "learn_events.jsonl"  # --- W2 v0.26.0: learn-events ---
RANK_CONFIG_FILE = DATA_DIR / "rank_config.json"  # --- W3 v0.27.0: last-good словарь синонимов ---
_ENV_TOKEN = bool((os.environ.get("MEMORY_TOKEN") or "").strip())
TOKEN = _resolve_token()
WEBHOOK_SECRET = os.environ.get("PLATEGA_WEBHOOK_SECRET", "")
PLATEGA_MERCHANT_ID = os.environ.get("PLATEGA_MERCHANT_ID", "")
PLATEGA_API_KEY = os.environ.get("PLATEGA_API_KEY", "")
PLATEGA_API = os.environ.get("PLATEGA_API_BASE") or (billing.DEFAULT_API_BASE if billing else "")
PLATEGA_CURRENCY = os.environ.get("PLATEGA_CURRENCY") or "RUB"
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "https://rezon-ai.ru").rstrip("/")
# Каталог релизных tarball'ов для автообновлений (v0.20.0): наполняет деплой-скрипт
# магазина (git archive тега + sha256). Наружу только чтение текущей VERSION —
# имя файла не приходит из запроса → path traversal невозможен.
RELEASES_DIR = os.environ.get("RELEASES_DIR") or str(DATA_DIR / "releases")
# NB: docker-compose подставляет ${VAR:-} → env ПУСТАЯ, а не отсутствует — поэтому `or`, а не get(default)
# Планы v0.13.0: только Pro, лесенка длительности. tier лицензии всегда "pro" —
# длительность живёт в days (→ exp ключа). Скидки от базы 149₽/мес:
# pro3 −11%, pro6 −16%, pro12 −27%. Цены/лейблы — единый источник plans.PRICES/LABELS.
_PLAN_DAYS = {"pro1": 31, "pro3": 93, "pro6": 186, "pro12": 372}
PLANS = {pid: {**plans.PRICES[pid], "days": _PLAN_DAYS[pid], "label": plans.LABELS[pid]}
         for pid in _PLAN_DAYS}
# v0.33.2: донаты — чистая поддержка проекта: days=0, без кода/ключа/лицензии,
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
# --- digiseller per-order (v0.7) --- /billing/digiseller/gen — код по заказу Digiseller
DIGISELLER_GEN_SECRET = os.environ.get("DIGISELLER_GEN_SECRET", "")
DIGISELLER_SIGN_SCHEME = os.environ.get("DIGISELLER_SIGN_SCHEME", "url-key")
DIGISELLER_RESPONSE_FORMAT = os.environ.get("DIGISELLER_RESPONSE_FORMAT", "text")
DIGISELLER_PRODUCT_ID = os.environ.get("DIGISELLER_PRODUCT_ID", "XXXXXXXX")
# --- digiseller уведомления (v0.9.1): /webhook/digiseller/{sale,message} → лог + Telegram ---
DIGISELLER_HOOK_SECRET = os.environ.get("DIGISELLER_HOOK_SECRET", "")
TG_NOTIFY_CHAT_ID = os.environ.get("TG_NOTIFY_CHAT_ID", "")
# STORE_MODE=1 — инстанс магазина (rezon-ai.ru): /api/license/activate убирается из публичных
STORE_MODE = (os.environ.get("STORE_MODE") or "").strip() == "1"
# READONLY=1 — read-only зеркало прода (resync.py перезаписывает memory.json каждые 5 мин):
# мутации памяти запрещены (иначе записи молча стираются ресинком), чтение работает
READONLY = (os.environ.get("READONLY") or "").strip() == "1"
WEBHOOKS_LOG = DATA_DIR / "webhooks.jsonl"
EMBEDDINGS_FILE = DATA_DIR / "embeddings.json"     # В2.2: {name: {"v": [...], "u": updatedAt}}
EMBED_STATE_FILE = DATA_DIR / "embed.state.json"   # В2.2: дневной кап токенов (len/4)
ACCESS_FILE = DATA_DIR / "access.json"  # В2.3: ACT-R счётчики обращений {name: {"n","t"}}
LICENSE_SECRET = licensing.get_secret(DATA_DIR)  # env LICENSE_HMAC_SECRET или DATA_DIR/.hmac_key
LICENSE_SUB_DAYS = 31  # легаси-fallback (digiseller без суммы); реальный срок = PLANS[plan]["days"]
EXTEND_DISCOUNT = 0.9  # продление −10% (решение владельца 2026-10-13) — применяется серверно в /api/billing/pay
# --- v0.37.0: реферальная система (SPEC-referral.md) ---
# Секрет деривации реф-кодов (HMAC от install_id). Прод — строго env; дефолт — только
# для тестов/dev (compose подставляет пустую строку → `or`, не get(default)).
REF_HMAC_SECRET = os.environ.get("REF_HMAC_SECRET") or "dev-only-ref-secret"  # dev-дефолт только для локалки
REF_DISCOUNT = 0.8   # −20% приглашённому на первый платёж тарифа (только Platega-контур)
REF_STACK_CAP = 4    # +10 п.п. за покупку приглашённого; кап 4 → продление до −50% (стек включает базу −10%)
PORT = int(os.environ.get("PORT", "8787"))
STARTED = time.time()
EMBED_ON = False
EVENTS_CAP = 10000  # максимум строк в events.jsonl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("maxgraph")

# L7: на магазине без явного секрета коды вычислимы офлайн по install_id — орём при старте
# (после log=…: warn до этого места = NameError при импорте, ре-ревизия N1)
if STORE_MODE and REF_HMAC_SECRET == "dev-only-ref-secret":
    log.warning("REF_HMAC_SECRET не задан в .env — реф-коды вычислимы офлайн; задай секрет!")

_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- storage ---

def _load():
    """Load graph from NDJSON -> (entities dict, relations list).

    Шарды (v0.34.0): memory.json (плоское) + projects/<slug>.json — читаются в те же
    структуры; read-путь дальше не знает про шарды. Связи с отсутствующим концом
    отбрасываются (суд 20261013-000330, У2: удалённый шард проекта не оставляет
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
                if rec.get("status"):  # Ф3: явный статус бага; legacy-строки без него не меняются
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


# Windows-reserved имена файлов (security-ревью v0.34.0, LOW): slug «con»/«nul»/…
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
    конца в нём; иначе плоская (правило «по концам», гриллинг владельца)."""
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
    """Шард-запись, двухфазно (суд У1): сначала ВСЕ .tmp, потом ВСЕ replace — окно
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
    """Одноразовая миграция на шарды (v0.34.0): монолитный memory.json →
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
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)  # v0.39.1: при плоской памяти _save
    # не создаёт projects/ — без этого сентинел падал FileNotFoundError (краш-луп 2026-10-15)
    sentinel.write_text("ok\n", encoding="ascii")  # маркер «миграция была»
    log.info("шардинг: memory.json разрезан (%d сущностей, %d связей), слепок → %s",
             len(ents), len(rels), backup.name)


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
    """Append event to the audit log (timeline). project — slug проекта (Ф1), если задан.
    source — кто породил событие: "" (default) — пользователь/агент/сервер;
    "hook" — захват-хук хоста (Ф5)."""
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


# ------------------------------------------------------------ ACT-R (В2.3) ---
# В2.3 (аналог ACT-R reinforcement у claude-mem): частые и недавние обращения
# поднимают сущность в выдаче — память сама учится, что важно. Счётчики
# in-memory (НЕ в memory.json: каждый search = fsync-шторм, а правка updatedAt
# сломала бы recency-скоринг и кэш searchrank, чей ключ — (name, updatedAt));
# flush в access.json раз в ACTR_FLUSH_SEC (daemon-поток actr_loop) и при
# shutdown (atexit, поток daemon'ный, graceful = atexit).
# Пульс НЕ пишется (фоновый тик засорял бы timeline), события flush — нет.
_actr = actr.Store()
_actr.load(ACCESS_FILE)  # старт: нет/битый access.json → с нуля (fail-open)


def _touch(names):
    """В2.3: учёт «осмысленных» чтений — search (top-20 выдачи), open_nodes,
    session_start (сущности, попавшие в context_block). НЕ read_graph/пустой
    query: дамп всего графа — не сигнал интереса к конкретным фактам.
    O(1) на имя, leaf-лок внутри Store (без I/O/сети/чужих локов — порядок
    локов не нарушается: _actr-лок не захватывается под _lock/_codes_lock)."""
    _actr.touch(names)


def _actr_boost_fn():
    """В2.3: actr_fn name→множитель ∈ [1, 1+ACTR_MAX_BOOST] для searchrank.rank
    и сортировок session_start; None, когда буст выключен (ACTR_MAX_BOOST=0) —
    rank тогда идёт веткой byte-identical 0.14.x/В2.2. Env читается на каждый
    вызов (как _hybrid_search у embed): конфиг подхватывается без рестарта."""
    cfg = actr.config_from_env()
    if cfg["max_boost"] <= 0.0:
        return None
    return actr.boost_fn(_actr, cfg)


# ------------------------------------------------------------- projects -----

# Ф1 (MEMORY-SYSTEM-v2): опциональный слой проектов. slug валидируется на границе
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
    """project из query GET: (slug, "") | ("", ошибка). Граница валидации (Ф1)."""
    return _check_project(request.query_params.get("project"))


def _project_entity_name(slug: str) -> str:
    return f"project:{slug}"


def _in_project(entity: dict, slug: str) -> bool:
    """Сущность адресуема в контексте проекта: её project == slug, либо это сама
    сущность проекта project:<slug> (она структурная, сама проекту не принадлежит)."""
    return (entity.get("project") or "") == slug or entity["name"] == _project_entity_name(slug)


def _filter_graph(ents: dict, rels: list, slug: str, all_projects: bool):
    """Изоляция Ф1: project=X → сущности проекта X (+ project:X), связи — оба конца
    видимы. Без project или all_projects=true — полный граф (обратная совместимость)."""
    if not slug or all_projects:
        return list(ents.values()), rels
    es = [e for e in ents.values() if _in_project(e, slug)]
    names = {e["name"] for e in es}
    return es, [r for r in rels if r["from"] in names and r["to"] in names]


# ------------------------------------------------------------- taxonomy -----

# Ф3 (MEMORY-SYSTEM-v2): канонические типы сущностей. Свободные типы НЕ ломаются:
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
    # В2.1 (work_state): открытые дела проекта
    "task": "task", "todo": "task", "задача": "task", "туду": "task", "дело": "task",
}

# Явный статус (Ф3 — bug, В2.1 — task): только перечисленные значения.
# default при создании — open.
BUG_STATUSES = ("open", "fixed")
TASK_STATUSES = ("open", "done")
# Типы, у которых status — первоклассное поле; для остальных — игнор с warning.
_STATUS_BY_TYPE = {"bug": BUG_STATUSES, "task": TASK_STATUSES}


def _canonical_type(value) -> str:
    """Нормализация entityType на границе записи (Ф3): словарный синоним (RU/EN,
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


# ------------------------------------------------------------- graph ops ----

def _norm_obs(s) -> str:
    """Ключ дедупликации фактов (эпик «устойчивость к компактизации», слой 2):
    регистр, повторные пробелы и краевая пунктуация не делают факт новым.
    Хранится оригинал строки — ключ нужен только для сравнения. Строка из одной
    пунктуации нормализуется в пустую → fallback на оригинал (security L5)."""
    r = " ".join(str(s).casefold().split()).strip(" .,;:!?…")
    return r or str(s).strip()


# --- В3.2 (срез 2, суд 20261011-153040): инвалидация устаревших фактов --------
# Устаревший факт НЕ удаляется, а помечается префиксом-маркером [~YYYY-MM-DD]
# (археология: текст остаётся в данных, delete_observations работает всегда — И3).
# Читающие пути: session_start и search показывают/скорят только валидные строки;
# read_graph/open_nodes — как есть (полная правда, включая помеченные).

_INVALIDATED_RE = re.compile(r"^\[~[0-9]{4}-[0-9]{2}-[0-9]{2}\]\s*")


def _is_invalidated(obs) -> bool:
    s = str(obs)
    return s.startswith("[~") and bool(_INVALIDATED_RE.match(s))  # fast-path


def _strip_marker_on_write(text):
    """Входящий факт с маркером [~дата] → маркер срезаем
    (иначе запись рождается невидимой для search/session_start — копипаст из
    read_graph при ре-прайме). → (текст, был_маркер)."""
    s = str(text)
    if _is_invalidated(s):
        return _INVALIDATED_RE.sub("", s, count=1), True
    return s, False


def _valid_obs(e: dict) -> list:
    """Наблюдения сущности без инвалидированных (Ч2)."""
    return [o for o in e.get("observations", []) if not _is_invalidated(o)]


def _mark_observation(obs: str, today: str) -> str:
    """Поставить маркер инвалидации (идемпотентно: уже помеченная — как есть)."""
    return obs if _is_invalidated(obs) else f"[~{today}] {obs}"



def _create_entities_graph(ents, rels, items, project: str = "", source: str = ""):
    """Ядро op_create_entities на УЖЕ загруженном графе (без _load/_save и без
    захвата _lock) — для батчей, где caller держит лок и граф на весь запрос
    (api_ingest SessionEnd, Ф8 LOW: раньше батч из N проектов делал N×_load +
    N×_save). → (out, changed); запись — обязанность caller'а, один _save в конце.

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
        if e["name"] in ents:
            ex = ents[e["name"]]
            ex_project = ex.get("project") or ""
            if project and ex_project and ex_project != project:
                warnings.append(
                    f"'{e['name']}' уже существует в проекте '{ex_project}' — merge "
                    f"в проект '{project}' отклонён (имена сущностей глобально уникальны)")
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
            # явный статус (Ф3 bug: open/fixed; В2.1 task: open/done), default open
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

    project (Ф1): новые сущности получают slug; сущность project:<slug> автосоздаётся
    один раз, каждая новая сущность связывается с ней «part of». Merge разрешён, если
    проект сущности совпадает или не задан (легаси-сущность тогда ПРИНИМАЕТ проект,
    счётчик adopted); конфликт проектов (X ≠ Y) → сущность не трогаем, она считается
    в skipped, в ответе warnings[] — имена сущностей глобально уникальны.

    Типы (Ф3): entityType канонизируется (синонимы RU/EN → канонический, см.
    CANONICAL_TYPES/_TYPE_SYNONYMS); свободный тип остаётся как есть. Новый bug
    получает явный status open/fixed, новый task (В2.1) — open/done; без
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
    # В3.3: подсказки к записи (Т3 similar + Т4 hygiene) — вне лока (embed =
    # HTTP к своему relay), только совет (И1). Тексты — факты сущности (не
    # entity_text как у neardup: name/title размывают containment) — отдельные
    # embed-вызовы, ≤_HINT_TEXTS_MAX на батч (шаринга LRU нет).
    created = out.get("created") or []
    touched = [e["name"] for e in created]
    # similar — по ФАКТАМ новой сущности (name/title — идентификаторы, в
    # containment не участвуют, иначе размывают сходство)
    new_texts = [" ".join(str(o) for o in e.get("observations", []))
                 for e in created[:_HINT_TEXTS_MAX]]
    hints = _quality_hints(ents, rels, touched, new_texts, project)
    if hints:
        out["hints"] = hints
    return out


_NEARDUP_COS = 0.85   # слой 2: порог косинуса «похожих сущностей»; ниже — молчим
_NEARDUP_MAX = 3      # сколько кандидатов показать в одном предупреждении
_NEARDUP_CHECK_MAX = 20  # security M1: подсказка best-effort — за один вызов
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
        cands = [n for n in scope if n in store]  # security L1: не весь store
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
        log.warning("neardup check skipped: %s", ex)  # security L3: было info —
        return []                                     # поломка embed не видна


# --- В3.3 (срез 3, суд 20261011-153040): подсказки качества записи ------------
# Т3 similar: ≤3 похожих записей на новые тексты (embed-косинус ≥0.92 через СВОЙ
# relay — И2; embed выключен/сбой/нет вектора → FTS-фоллбэк: containment стемов
# ≥0.8, ≥3 стемов). Т4 hygiene: «15+ наблюдений — разделить?», «нет связей»,
# «нет тегов» — ≤3. И1: ТОЛЬКО совет — граф и пульс не меняются, запись не
# блокируется, любой сбой → подсказок нет (учёт embed-квоты в embed.state.json —
# это счётчик сервиса, не память). Вызывать ВНЕ _lock (embed = HTTP).

_HINT_SIMILAR_COS = 0.92      # Т3: «почти дубликат» по эмбеддингам (порог плана)
_HINT_SIMILAR_CONTAIN = 0.8   # FTS-фоллбэк: доля стемов нового текста в сущности
_HINTS_MAX = 3                # не больше трёх подсказок каждого вида (суд)
_HINT_TEXTS_MAX = 5           # новых текстов на вызов проверяем (кап embed/API)
_HINT_OBS_SPLIT = 15          # Т4: «много наблюдений — разделить?»


def _similar_hints(new_texts, ents, project, self_names) -> list:
    """Т3: [{text, similar_to, title, score, via}] ≤_HINTS_MAX. Кандидаты — сущности
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
        scope = []  # один проход: (имя, сущность-с-валидными-obs для Ч2)
        for n, e in ents.items():
            if n in self_names or (project and not _in_project(e, project)):
                continue
            if any(_is_invalidated(o) for o in e.get("observations", [])):
                e = dict(e, observations=_valid_obs(e))
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
        log.warning("similar hints skipped: %s", ex)  # И1: подсказка не роняет запись
        return []


def _hygiene_hints(ents, rels, touched_names) -> list:
    """Т4: [{entity, hint}] ≤_HINTS_MAX — механические правила гигиены графа."""
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
    """В3.3: {"similar": [...], "hygiene": [...]} — только непустые части.
    И1: подсказки не роняют запись — любой сбой → {} (двойная страховка над
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
    project (Ф1): сущность адресуема только внутри своего проекта.
    Ф3: entityType канонизируется (синонимы → канонический; свободный — как есть).
    status — для bug (open/fixed) и task (open/done, В2.1): для остальных типов
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
        # В3.2 (Т2, откат supersedes — признанный пробел суда): restore=[строки]
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
                        and (o == s or o.strip() == s
                             or _INVALIDATED_RE.sub("", o, count=1).strip() == s)), None)
            if idx is None:
                restore_nf.append(s)
                continue
            e["observations"][idx] = _INVALIDATED_RE.sub("", e["observations"][idx], count=1)
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


def op_add_observations(items, project: str = ""):
    """project (Ф1): сущность адресуема только внутри своего проекта — чужая
    сущность с тем же именем считается ненайденной (имена глобально уникальны).
    Дедуп — по _norm_obs (регистр/пробелы/краевая пунктуация), и против
    имеющихся, и внутри входящего списка; хранится оригинал строки.

    В3.2 (Т2): item["supersedes"] — список УСТАРЕВШИХ строк этой же сущности:
    каждая помечается маркером [~YYYY-MM-DD] и остаётся в данных (археология;
    удаление — delete_observations всегда, И3). Совпадение — точное по сырой
    строке; уже помеченная — идемпотентный пропуск; ненайденные — в
    supersedesNotFound ответа (молча не теряем)."""
    today = now_iso()[:10]
    with _lock:
        ents, rels = _load()
        results, errors = [], []
        ts = now_iso()
        for item in items:
            name, contents = item["entityName"], item.get("contents", [])
            if name not in ents or (project and not _in_project(ents[name], project)):
                suffix = f" in project '{project}'" if project else ""
                errors.append(f"Entity '{name}' not found{suffix}")
                continue
            obs_list = ents[name]["observations"]
            # Т2: инвалидация устаревших строк (до добавления новых — новый факт
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
            marked, sup_nf = [], []
            for s in sup:
                s = str(s or "").strip()
                if not s:
                    continue
                idx = next((i for i, o in enumerate(obs_list)
                            if o == s or str(o).strip() == s), None)
                if idx is None:
                    # уже помечена ранее? (ищем по тексту без маркера) → идемпотентно
                    if any(_is_invalidated(o) and _INVALIDATED_RE.sub("", o, count=1).strip() == s
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
            have = {_norm_obs(o) for o in obs_list}
            added = []
            stripped = 0
            for c in contents:
                c, was_marked = _strip_marker_on_write(c)
                stripped += 1 if was_marked else 0
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
            results.append(r)
            for c in added:
                _event("add_observation", name, c[:120],
                       project=project or (ents[name].get("project") or ""))
        if results:
            _save(ents, rels)
        out = {"results": results}
        if errors:
            out["errors"] = errors
        _reprime_tick(project, out)
        touched = [r["entityName"] for r in results]
        new_texts = [c for r in results for c in r["addedObservations"]]
    # В3.3: подсказки ПОСЛЕ записи, вне лока (embed = HTTP); только совет (И1)
    hints = _quality_hints(ents, rels, touched, new_texts, project)
    if hints:
        out["hints"] = hints
    return out


def op_delete_entities(names, project: str = ""):
    """project (Ф1): удаляются только сущности этого проекта — чужие с тем же
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


def op_delete_observations(items, project: str = ""):
    """project (Ф1): затрагиваются только сущности этого проекта."""
    with _lock:
        ents, rels = _load()
        removed = 0
        for item in items:
            name = item["entityName"]
            if name not in ents or (project and not _in_project(ents[name], project)):
                continue
            drop = set(item.get("observations", []))
            before = len(ents[name]["observations"])
            ents[name]["observations"] = [o for o in ents[name]["observations"] if o not in drop]
            removed += before - len(ents[name]["observations"])
            if before != len(ents[name]["observations"]):
                ents[name]["updatedAt"] = now_iso()
        _save(ents, rels)
        out = {"removed": removed}
        _reprime_tick(project, out)
        return out


def op_delete_relations(items, project: str = ""):
    """project (Ф1): удаляются только связи, чьи ОБА конца видимы в проекте —
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


def op_read_graph(project: str = "", all_projects: bool = False):
    with _lock:
        ents, rels = _load()
    es, rs = _filter_graph(ents, rels, project, all_projects)
    return {"entities": es, "relations": rs}


def _hybrid_search(query_str: str):
    """В2.2: (vector_weight, cosine_fn) для searchrank.rank; (0.0, None) — чистый FTS.

    Гибрид применяется ТОЛЬКО когда embed включён (EMBED_URL+EMBED_KEY) И у
    запроса есть вектор (LRU-кэш в embed; промах = один /embeddings вызов с
    EMBED_TIMEOUT; сбой/кап → None). Любой сбой → (0.0, None): поведение
    byte-identical 0.14.x. Корпус уже отфильтрован (Ф1/Ф3) ДО вызова rank —
    косинусы считаются только по именам из видимого корпуса (изоляция
    проектов сохраняется, вектора чужих проектов не читаются)."""
    cfg = embed.config_from_env()
    if not embed.enabled(cfg):
        return 0.0, None
    w = cfg["hybrid_weight"]
    if w <= 0.0:
        return 0.0, None  # w=0 → чистый FTS: запрос даже не эмбеддим (не жжём API)
    store = embed.load_store(EMBEDDINGS_FILE)  # stat-only при тёплом кэше
    if not store:
        return 0.0, None  # пополнитель ещё не наполнил — не жжём API на запрос
    qv = embed.query_vector(query_str, cfg=cfg, state_path=EMBED_STATE_FILE)
    if qv is None:
        return 0.0, None
    return w, embed.cosine_scorer(store, qv)


def op_search(query, project: str = "", all_projects: bool = False, type_filter: str = "",
              limit: int = searchrank.DEFAULT_LIMIT):
    """Поиск. Непустой query (Ф6) — ранжированный FTS-lite (searchrank): BM25-подобный
    скор по полям name×3/title×2/tags×1.5/observations×1 + фразовый бонус + recency ≤10%,
    топ-limit (default 20); у сущностей ответа + score, snippet, highlights (позиции
    совпадений в snippet — без HTML, безопасно для esc()). Ранжирование идёт внутри
    видимого корпуса: Ф1 (project/all_projects) и Ф3 (type= после нормализации)
    комбинируются как фильтры корпуса ДО скоринга.

    В2.2 (опционально): при заданных EMBED_URL+EMBED_KEY — гибрид FTS+косинус
    эмбеддингов (searchrank.rank(vector_weight=HYBRID_VECTOR_WEIGHT, default 0.6);
    корпус ≤2000 → косинус по всем (full-scan), больше → FTS top-200 предфильтр;
    score ответа становится blended 0..1).
    Без эмбеддингов (выключен/сбой/нет векторов) — чистый FTS 0.14.x.

    В2.3 (default ON, отключается ACTR_MAX_BOOST=0): ACT-R буст — часто и недавно
    читаемые сущности получают множитель ≤ 1+ACTR_MAX_BOOST (default +25%) к
    итоговому скору (FTS или blended); top-20 выдачи учитывается как обращение
    (_touch). Счётчики глобальны по имени, но имена глобально уникальны и буст
    применяется после фильтра корпуса (Ф1/Ф3) — изоляция проектов сохраняется.

    Пустой query — старое поведение: все сущности (с фильтрами), без score-полей,
    без лимита. Query без извлекаемых терминов (пунктуация, CJK — вне алфавита
    токенизатора) — фоллбек на старое подстрочное совпадение (без score-полей).

    Сущности ответа несут канонический type; bug — ещё и status (явный, а для legacy
    без поля — выведенный эвристикой _bug_is_open); task (В2.1) — status
    (явный open/done, битое/отсутствующее хранимое значение → "open")."""
    tf = _canonical_type(type_filter) if str(type_filter or "").strip() else ""
    with _lock:
        ents, rels = _load()
    corpus = list(ents.values())
    if project and not all_projects:
        corpus = [e for e in corpus if _in_project(e, project)]
    if tf:
        corpus = [e for e in corpus if _canonical_type(e.get("entityType")) == tf]
    # Ч2 (В3.2): инвалидированные ([~дата]) наблюдения исключаются из поиска —
    # ни из скоринга (FTS), ни из сниппета, ни из полей ответа (устаревшая цена
    # не должна всплывать). Полная правда с маркерами — read_graph/open_nodes.
    # Векторная составляющая гибрида — по последнему embed-снимку (маркер
    # попадёт туда при ближайшем refresh после updatedAt-бампа). Копии только
    # для затронутых сущностей, один проход.
    _corpus, _changed = [], False
    for e in corpus:
        obs = e.get("observations", [])
        if any(_is_invalidated(o) for o in obs):
            _corpus.append(dict(e, observations=[o for o in obs
                                                 if not _is_invalidated(o)]))
            _changed = True
        else:
            _corpus.append(e)
    if _changed:
        corpus = _corpus
    query_str = str(query or "")
    # W3 (v0.27.0): FTS-ветка расширяется словарём синонимов (rank/tokenize/snippet
    # согласованно); векторный запрос остаётся исходным (решение W3-Q3).
    expanded_str = rankcfg.expand_query(query_str)
    qts = searchrank.tokenize(expanded_str)
    if query_str.strip() and qts:
        vec_w, cos_fn = _hybrid_search(query_str)  # В2.2: (0.0, None) → чистый FTS
        pairs = searchrank.rank(corpus, expanded_str, limit=limit,
                                vector_weight=vec_w, cosine_fn=cos_fn,
                                actr_fn=_actr_boost_fn())  # В2.3: None → без изменений
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
            # В2.1: контракт ответа — open|done; задачи через границу записи всегда
            # имеют явный status (default open), прочее → «не закрыта»
            v["status"] = _task_status_out(e)
        if ranked:
            v["score"] = round(s, 4)
            text, highlights = searchrank.snippet(e, qts)
            v["snippet"] = text
            v["highlights"] = highlights
        out_entities.append(v)
    if query_str.strip():
        # В2.3: «осмысленное» чтение — top-20 выдачи (ranked и CJK-фоллбек).
        # Пустой query (дамп корпуса) не учитываем — как read_graph, это не
        # сигнал интереса к конкретным фактам (и это O(N) касаний на вызов).
        _touch([e["name"] for e in out_entities[:actr.TOUCH_TOP]])
    # W2 learn-events: источник связки search→open. Только память, сети нет;
    # не-ранжированные ветки (пустой query, CJK-фоллбек) сбрасывают связку.
    if ranked:
        learn.record_search(query_str,
                            [(e["name"], e.get("title"))  # без фоллбэка на name
                             for e in out_entities])
    else:
        learn.record_search("", [])
    return {"entities": out_entities, "relations": mrels}


def op_open(names, project: str = ""):
    want = set(names)
    with _lock:
        ents, rels = _load()
    if not project:
        # как раньше: связи — оба конца среди запрошенных имён (даже если узла нет)
        found = [e for n, e in ents.items() if n in want]
        frels = [r for r in rels if r["from"] in want and r["to"] in want]
        _touch([e["name"] for e in found])  # В2.3: явное открытие = обращение
        learn.record_open([e["name"] for e in found])  # W2: связка со свежим поиском
        return {"entities": found, "relations": frels}
    found = [e for n, e in ents.items() if n in want and _in_project(e, project)]
    found_names = {e["name"] for e in found}
    frels = [r for r in rels if r["from"] in found_names and r["to"] in found_names]
    _touch([e["name"] for e in found])  # В2.3: только реально найденные имена
    learn.record_open([e["name"] for e in found])  # W2: связка со свежим поиском
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


# ---------------------------------------------------- work_state (В2.1) -----

# В2.1 (аналог work_state у claude-mem): открытые дела проекта — сущности
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
    не закрыл — открыта», как у legacy-багов Ф2)."""
    return str(e.get("status") or "").strip().lower() != "done"


def _task_status_out(e: dict) -> str:
    """status задачи для ответов наружу (open|done): хранимый валидный, иначе
    «не закрыта» (open) — контракт как у bug (см. op_search)."""
    st = str(e.get("status") or "").strip().lower()
    return st if st in TASK_STATUSES else "open"


def op_task_add(title, note: str = "", project: str = "", source: str = "",
                links=()) -> dict:
    """Создать задачу (В2.1): entityType task, status open, имя task-<slug(title)>
    (коллизия → -2…), note — первый факт (опционально). Проектная семантика
    (автосоздание project:<slug>, part-of) — из _create_entities_graph.
    В3.4 (Т5): links=[имена] — связи «relates to» к существующим сущностям
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
            # Т5: связи в том же вызове (Ф1: только внутри проекта, как Л5 В3.1)
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
    """Закрыть задачу (В2.1): status=done + событие task_done в пульс.
    Только entityType=task своего проекта: отсутствующая/чужая → not found,
    не-task → error. Уже закрытая — идемпотентно {"updated", "already": True}
    без повторного события.
    В3.4 (Т5): при закрытии — связь «discussed» от session-сущности сегодняшнего
    дня к задаче, если сессия дня существует (stub SessionEnd-хука тоже считается;
    новую сессию НЕ создаём — И1, авто-записей нет). Ответ: + linked_to_session."""
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
        # Т5: связь к сессии дня (вечерняя запись «что сделано» знает свои задачи)
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
    задачи (Ф1); без project — все открытые задачи графа (плоское чтение, как
    read_graph без проекта). → {"tasks": [...], "total": N}.
    Ч2-контракт: task_list — «полная правда», как
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


# --- В3.1 (суд 20261011-153040): закрытие сессии одним вызовом + напоминание --
# Т1/Ч1 из волны 2: session_close создаёт/мерджит session-сущность дня с итогами
# и discussed-связями за один вызов (вместо 3 забываемых), а session_start
# напоминает о незакрытой сессии. Условия суда-инварианты: напоминание и
# авто-сбор связей из пульса — МЕХАНИЧЕСКИЕ (без LLM) и НИЧЕГО не пишут за
# агента (напоминание — только строка в context_block; авто-линки — лишь внутри
# явного вызова session_close самим агентом).

_SESSION_CLOSE_TAG = "session-close"  # «сессия закрыта агентом с итогами»
_SESSION_LINKS_MAX = 20               # потолок discussed-связей за вызов
_UNCLOSED_MIN_WRITES = 5              # Ч1: напоминание при ≥5 пишущих событиях за день
_UNCLOSED_TOUCHED_MAX = 5             # Ч1: сколько имён затронутых показать
_WRITE_ACTIONS = frozenset({          # пишущие события пульса (для Ч1 и авто-лinкс)
    "create_entity", "merge_entity", "create_relation", "update_entity",
    "add_observation", "delete_entity", "task_done", "session_close",
})


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
    """Закрыть сессию дня одним вызовом (В3.1): session-сущность дня (merge в уже
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
            have = set(e["observations"])
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
                # Ф1: явные связи тоже внутри проекта
                if l in ents and _in_project(ents[l], project):
                    want.append(l)
                else:
                    skipped.append(l)
            auto = False
        else:
            want, skipped, auto = touched, [], True
        if len(want) > _SESSION_LINKS_MAX:  # Л3: хвост не теряем молча
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
        # Л4: пустой повторный вызов (ничего не добавилось) — без _save и без
        # события в пульс, ответ идемпотентный
        if changed or linked:
            _save(ents, rels)
        out = {"session": name, "merged": merged, "already_closed": already,
               "linked": linked, "links_auto": auto, "links_skipped": skipped,
               "open_tasks_hint": hint}
        _reprime_tick(project, out)
    if changed or linked:
        _event("session_close", name, obs[0][:120], project=project, source=source)
    return out


def _open_tasks_from_data(data) -> list:
    """SessionEnd data.open_tasks (В2.1) → нормализованные title'ы. Ввод
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


# ------------------------------------------------------ session_start (Ф2) --

# Ф2 (MEMORY-SYSTEM-v2): бюджетная инъекция контекста проекта — аналог
# SessionStart-блока claude-mem (~50 наблюдений + ~10 сессий, жёсткий бюджет
# ~10k chars). Операция чтения: событие в пульс НЕ пишем, лог-строку — да.
SESSION_START_BUDGET = 10000   # default budget_chars
_SS_CAP_PROJECT_FACTS = 5      # 3-5 ключевых фактов сущности project:<slug>
_SS_CAP_DECISION_LINES = 50    # строк наблюдений decisions (дословно)
_SS_CAP_BUG_LINES = 30         # строк открытых багов
_SS_CAP_TASK_LINES = 10        # строк открытых задач (В2.1)
_SS_CAP_SESSIONS = 10          # прошлых сессий
_SS_CAP_EVENTS = 15            # событий пульса
# Запас под маркер усечения: «… (ещё N, усечено бюджетом)\n» ≤ 32 chars, т.к.
# N ≤ сумме кэпов (120); берём с запасом — иначе маркер мог бы превысить бюджет.
_SS_MARKER_RESERVE = 40

# Эвристика статуса бага — ФОЛЛБЕК для legacy-багов без явного поля status (Ф3):
# последнее статусное слово в фактах («status: fixed», «закрыт»…) или тег
# (closed / status:open). Новые баги несут явный status open/fixed (Ф3).
_BUG_STATUS_WORD = re.compile(
    r"\b(open|reopened|fixed|closed|resolved|done|открыт\w*|закрыт\w*|исправлен\w*|пофикш\w*)\b",
    re.IGNORECASE)
_BUG_OPEN_WORDS = {"open", "reopened"}


def _bug_word_is_open(word: str) -> bool:
    return word in _BUG_OPEN_WORDS or word.startswith("открыт")


def _bug_is_open(e: dict) -> bool:
    """Открыт ли баг. Ф3: ЯВНЫЙ status (open/fixed) решает первым. Legacy-баг без
    status (или с неизвестным значением) — эвристика Ф2: закрыт, если тег
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
        for m in _BUG_STATUS_WORD.finditer(str(obs)):
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
    поведение (_recency_key, byte-identical). С mult_fn (В2.3 ACT-R) ключ —
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
    """Бюджетная инъекция контекста проекта (Ф2): {context_block, meta}.

    Секции по приоритету (обрезка бюджетом — снизу вверх, от последней; порядок
    жертвования: events → sessions → unclosed → tasks → bugs → decisions):
      # Проект: <slug>    — title + до 5 ключевых фактов project:<slug>
      ## Решения владельца — entityType=decision, recency-first, дословно (≤50 строк)
      ## Открытые баги    — entityType=bug, явный status≠fixed (Ф3); legacy без
                            status — эвристика тега/статусного слова (≤30 строк)
      ## Открытые задачи  — entityType=task, status≠done (В2.1 work_state, ≤10
                            строк): title + первый факт; дела важнее истории,
                            поэтому выше сессий
      ## Незакрытая сессия — В3.1 (1 строка, только если ≥5 пишущих событий
                            сегодня и сессия дня без тега session-close);
                            механическое напоминание, ничего не пишет
      ## Прошлые сессии   — entityType=session, recency-first (≤10): имя + первые 2 факта
      ## Недавние события — последние ≤15 событий пульса проекта
    Пустые секции (кроме первой) опускаются; пустой проект → блок-подсказка
    «начни с create_entities». meta.sections[key] = {items, total}: items —
    строк/единиц секции в блоке, total — всего кандидатов в графе; расхождение
    без truncated=true означает срабатывание кэпа секции, а не бюджета.

    В2.3 (ACT-R, default ON; ACTR_MAX_BOOST=0 → выкл., byte-identical):
    внутри секций decisions/bugs/tasks сортировка — recency × множитель
    частоты обращений (≤+25%); сущности, попавшие в итоговый context_block
    (после бюджетной обрезки), учитываются как обращения (_touch). Сессии —
    чистая хронология (свежесть сессии важна сама по себе), секция событий —
    журнал, не чтение фактов сущности: ни буста, ни учёта.
    """
    try:
        budget = max(0, int(budget_chars))
    except (TypeError, ValueError):
        budget = SESSION_START_BUDGET
    pname = _project_entity_name(project)
    mult_fn = _actr_boost_fn()  # В2.3: None → сортировки byte-identical 0.14.x
    with _lock:
        ents, _rels = _load()
        ev_lines = (EVENTS_FILE.read_text(encoding="utf-8").splitlines()
                    if EVENTS_FILE.exists() else [])
        _reprime_writes[project] = 0  # слой 2: свежий прайм — счётчик с нуля
    proj_ent = ents.get(pname)
    proj_ents = [e for e in ents.values() if (e.get("project") or "") == project]
    # Ч2 (В3.2): инвалидированные ([~дата]) строки в контекст не попадают
    proj_facts = _valid_obs(proj_ent) if proj_ent else []

    # события проекта: один проход файла — счёт + последние N (кольцевой буфер)
    # + Ч1 (В3.1): счётчик СЕГОДНЯШНИХ пишущих событий для напоминания
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
              "unclosed": 0}  # В3.1: 1 — напоминание «незакрытая сессия» показано
    sections = []  # (key, header, lines) — только секции с контентом (+ всегда project)
    sec_names = []  # В2.3: имя сущности каждой строки sections ("" = без сущности)

    if not proj_ents and not proj_facts and not total_events:
        notice = (f"Проект «{project}» новый или пуст: в памяти по этому slug пока ничего нет. "
                  f'Начни с create_entities(project="{project}") — сущность проекта создастся '
                  "автоматически, факты добавляй через add_observations. Этот блок наполнится "
                  "после первых записей.")
        totals["project"] = 1  # единственная «строка контента» — сама подсказка
        sections.append(("project", header, [notice]))
        sec_names.append([""])  # подсказка — не сущность, обращения нет
    else:
        plines = [f"- {f}" for f in proj_facts[:_SS_CAP_PROJECT_FACTS]]
        sections.append(("project", header, plines))
        sec_names.append([pname] * len(plines))  # факты — это строки project:<slug>

        # В2.3: внутри decisions/bugs/tasks — recency × ACT-R множитель
        # (mult_fn=None → ровно старый recency-first).
        decisions = _ss_sort_recent(
            (e for e in proj_ents if e.get("entityType") == "decision"), mult_fn)
        dlines, dnames = [], []
        for e in decisions:
            obs_valid = _valid_obs(e)  # Ч2: инвалидированные решения не показываем
            totals["decisions"] += len(obs_valid)
            prefix = _ss_head(e) + ": "
            for obs in obs_valid:
                if len(dlines) >= _SS_CAP_DECISION_LINES:
                    break
                dlines.append(prefix + str(obs))
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
            obs_valid = _valid_obs(e)  # Ч2
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
                blines.append(head + ": " + str(obs))
                bnames.append(e["name"])
            if len(blines) >= _SS_CAP_BUG_LINES:
                break
        if blines:
            sections.append(("bugs", "## Открытые баги", blines))
            sec_names.append(bnames)

        # В2.1 (work_state): открытые задачи — после багов, перед сессиями
        # (дела важнее истории); бюджетом режутся после сессий, до багов.
        tasks = _ss_sort_recent(
            (e for e in proj_ents
             if e.get("entityType") == "task" and _task_is_open(e)), mult_fn)
        totals["tasks"] = len(tasks)
        tlines, tnames = [], []
        for e in tasks[:_SS_CAP_TASK_LINES]:
            line = _ss_head(e)
            obs_valid = _valid_obs(e)  # Ч2: первый ВАЛИДНЫЙ факт как note
            if obs_valid:
                line += ": " + str(obs_valid[0])
            tlines.append(line)
            tnames.append(e["name"])
        if tlines:
            sections.append(("tasks", "## Открытые задачи", tlines))
            sec_names.append(tnames)

        # Ч1 (В3.1): «незакрытая сессия» — сегодня были пишущие события, но
        # session-сущности дня с итогами (тег session-close) нет. Только строка
        # в context_block: НИЧЕГО не записывается (инвариант суда И1). Секция —
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
            facts2 = [str(o) for o in _valid_obs(e)[:2]]  # Ч2
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
            # Ч2: detail add_observation несёт сырой
            # текст факта — инвалидированная строка не должна жить в контексте
            # через журнал; действие/сущность/время остаются
            if det and ev.get("action") != "add_observation":
                line += f" — {det}"
            elines.append(line)
        if elines:
            sections.append(("events", "## Недавние события", elines))
            sec_names.append([""] * len(elines))  # журнал — не чтение фактов

    text, shown, truncated = _fit_budget(sections, budget)
    # В2.3: обращениями считаются сущности, реально попавшие в context_block
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
    log.info("session_start: project=%s budget=%d used=%d truncated=%s",
             project, budget, len(text), truncated)
    return {"context_block": text, "meta": meta}


# --------------------------------------------------------------- schemas ----

class EntityIn(BaseModel):
    name: str
    entityType: str = "general"
    title: str = ""
    tags: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    status: str = ""  # Ф3/В2.1: только bug (open/fixed) и task (open/done), default open


class RelationIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    relationType: str


class ObservationsIn(BaseModel):
    entityName: str
    contents: list[str] = Field(default_factory=list)
    # В3.2 (Т2): устаревшие строки этой сущности — пометить [~дата], не удалять
    supersedes: list[str] = Field(default_factory=list)


class DeletionIn(BaseModel):
    entityName: str
    observations: list[str] = Field(default_factory=list)


# ------------------------------------------------------------- licensing ---

def _license_status() -> dict:
    """licensing.status с fail-closed при внутренней ошибке + свежая версия магазина (v0.18.2)."""
    try:
        st = licensing.status(DATA_DIR, LICENSE_SECRET)
        latest = licensing.latest_version(DATA_DIR, LICENSE_SECRET)
        st["latest"] = latest
        st["update_available"] = licensing.version_newer(latest, VERSION)
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

RU: у тебя есть постоянная память. Старт сессии — session_start(project=<slug>) — бюджетный блок контекста (read_graph — если нужен весь граф, search_nodes — по теме); после сжатия контекста (чекпоинт/summary от харнесса) — сразу повтори session_start; факты пиши СРАЗУ (create_entities/add_observations/create_relations, связи в активном залоге, name=kebab-case навсегда); конец сессии — session_close(project, summary, done, links) одним вызовом. Полный протокол: docs/PROTOCOL.md."""

mcp = FastMCP("maxgraph", host="0.0.0.0", port=PORT, streamable_http_path="/mcp",
              stateless_http=True, instructions=_MCP_INSTRUCTIONS)


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
    """Add new observations to existing entities. Одно наблюдение = один атомарный факт (с датой, если важно). Пиши СРАЗУ, как узнал, — не в конце разговора. В3.2: факт УСТАРЕЛ (цена, версия, решение) — не удаляй и не дублируй: передай старую строку в supersedes того же item (пометится [~дата] и уйдёт из поиска/session_start, оставшись в архиве; вернуть — update_entity(restore=[...]); физически удалить можно всегда — delete_observations). Ненайденные строки — в supersedesNotFound ответа. project: kebab-slug — сущности чужого проекта считаются ненайденными."""
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
                 limit: int = 20) -> dict:
    """Search for nodes by query. Используй вместо read_graph, когда нужна одна тема, и ПЕРЕД create_entities — чтобы не создать дубликат. project: kebab-slug — искать только в этом проекте; all_projects=true — явный кросс-поиск. type: фильтр по типу после нормализации (канонические: project/session/bug/decision/request/event/task; «bugfix»/«баг» → bug; свободный тип — точное совпадение). Непустой query — ранжированный FTS-lite (Ф6): сортировка по релевантности, limit — топ-N (default 20), у сущностей ответа + score, snippet, highlights (позиции совпадений в snippet, без HTML); часто и недавно читаемые сущности дополнительно всплывают (ACT-R, В2.3). Пустой query — все сущности с фильтрами, как раньше. Ответ включает канонический type, а для bug/task — status (open/fixed и open/done)."""
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_search(query, slug, all_projects, type, limit)


@mcp.tool()
def open_nodes(names: list[str], project: str = "") -> dict:
    """Open specific nodes in the knowledge graph by their names. project: kebab-slug — видимы только сущности этого проекта"""
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_open(names, slug)


@mcp.tool()
def list_timeline(limit: int = 50, project: str = "", all_projects: bool = False) -> dict:
    """List recent memory events (audit log): what was created/updated and when. project: kebab-slug — только события этого проекта; all_projects=true — явно все"""
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_timeline(limit, slug, all_projects)


@mcp.tool()
def session_start(project: str = "", budget_chars: int = 10000) -> dict:
    """ВЫЗЫВАЙ ПЕРВЫМ на старте сессии: бюджетный блок контекста проекта — {context_block, meta}. context_block готов к вставке в промпт: # Проект → ## Решения владельца (decision, дословно) → ## Открытые баги (bug, не закрытые) → ## Открытые задачи (task, status≠done — дела на потом) → ## Прошлые сессии (session, имя + 2 факта) → ## Недавние события. Внутри решений/багов/задач порядок — актуальность: свежесть × частота обращений (ACT-R, В2.3). Жёсткий бюджет budget_chars (default 10000): блок никогда его не превышает, усечение построчное с маркером «… (ещё N, усечено бюджетом)», флаг meta.truncated. project ОБЯЗАТЕЛЕН (kebab-slug) — без него весь граф намеренно не читается."""
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
    """Update title, type, tags or status of an existing entity. entityType канонизируется (синонимы RU/EN → project/session/bug/decision/request/event/task; свободный тип — как есть). status — для bug (open/fixed) и task (open/done); для остальных типов — игнор с warning в ответе. restore (В3.2): снять маркер [~дата] с наблюдений (откат supersedes) — строки с маркером или без; ненайденные — в restoreNotFound. project: kebab-slug — сущности чужого проекта считаются ненайденными"""
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


# --- В2.1 (work_state): открытые задачи проекта — task_add/task_done/task_list ---

@mcp.tool()
def task_add(title: str, note: str = "", links: list[str] | None = None, project: str = "") -> dict:
    """Добавить открытое дело «на потом» (work_state): создаёт task-сущность task-<slug(title)> со status=open (коллизия имени → суффикс -2). note — первый факт (опционально: контекст, ссылка). links — имена связанных сущностей (баг, решение, сессия): связи «relates to» создаются в том же вызове, неизвестные имена → links_skipped (В3.4). Пока задача открыта, она попадает в секцию «Открытые задачи» session_start. Закрытие — task_done (само свяжет задачу к сессии дня). project: kebab-slug проекта."""
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

_DASH_LOCAL = Path(__file__).with_name("dashboard.html")
_DASH_DATA = DATA_DIR / "dashboard.html"  # hot-override без пересборки образа
_PAGES = {"dashboard.html": _DASH_DATA, "landing.html": DATA_DIR / "landing.html",
          "cabinet.html": DATA_DIR / "cabinet.html", "billing.html": DATA_DIR / "billing.html",
          "privacy.html": DATA_DIR / "privacy.html", "offer.html": DATA_DIR / "offer.html"}
# admin.html нет в _PAGES с v0.41.0: страница версионируется в репо (была
# hot-override-only, master/прод разошлись). DATA_DIR/admin.html на сервере
# НЕ должен шэдоуить репозиторную — иначе деплой отдаёт старый JS под новый API.


def _page_html(name: str) -> str:
    override = _PAGES.get(name)
    if override is not None and override.exists():
        return override.read_text(encoding="utf-8")
    return (Path(__file__).parent / name).read_text(encoding="utf-8")


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

# Дашборд: connect-src + магазин (покупка Pro из браузера, v0.18.0). PUBLIC_BASE_URL
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
    return JSONResponse(op_read_graph(slug, _truthy(request.query_params.get("all_projects"))),
                        headers=_NOCACHE)


@mcp.custom_route("/api/export", methods=["GET"])
async def api_export(request: Request):
    """Полный бэкап: состояние графа + журнал событий. ?project=<slug> — только этот проект."""
    slug, err = _query_project(request)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    with _lock:
        ents, rels = _load()  # объединение шардов (v0.34.0: файл на диске уже не полная картина)
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


@mcp.custom_route("/api/search", methods=["GET"])
async def api_search(request: Request):
    """?q= — Ф6: непустой запрос → ранжированный FTS-lite (score/snippet/highlights,
    топ-limit, default 20, cap 500); пустой — все сущности с фильтрами, как раньше."""
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
    return JSONResponse(await asyncio.to_thread(
        op_search, request.query_params.get("q", ""), slug,
        _truthy(request.query_params.get("all_projects")),
        request.query_params.get("type", ""), limit))


@mcp.custom_route("/api/timeline", methods=["GET"])
async def api_timeline(request: Request):
    slug, err = _query_project(request)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    limit = int(request.query_params.get("limit", "100"))
    return JSONResponse(op_timeline(min(limit, 500), slug,
                                    _truthy(request.query_params.get("all_projects"))))


def _embed_detail():
    """v0.40.0: деталь семантики для дашборда — источник (env=свой endpoint,
    relay=облако магазина) или причина «выкл». v0.41.0: причина telemetry_off
    удалена — выключателя TELEMETRY=0 больше нет."""
    src = reason = None
    if EMBED_ON:
        src = "env" if (os.environ.get("EMBED_URL") or "").strip() else "relay"
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
        "fileBytes": _graph_file_bytes(),  # v0.34.0: memory.json + шарды projects/
        "license": {"state": lic.get("state"), "daysLeft": lic.get("days_left")},
        "embed": EMBED_ON,
        "embedDetail": _embed_detail(),  # v0.40.0
    }
    if slug:
        out["project"] = slug
    return JSONResponse(out)


def _body_project(body) -> tuple[str, str]:
    """project из JSON-тела POST: (slug, "") | ("", ошибка). Граница валидации (Ф1)."""
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


# --- В2.1 (work_state): REST-аналоги task_add/task_done/task_list ---

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
    links = body.get("links")  # В3.4 (Т5): список строк или отсутствует
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
    """В3.1: REST-аналог session_close — {summary, done[], links[], project}."""
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
            sup = it.get("supersedes")
            if sup is not None and not isinstance(sup, (list, str)):
                return JSONResponse({"error": "supersedes must be an array of strings"},
                                    status_code=400)
    return JSONResponse(op_add_observations(items, slug))


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


# --- Ф5 (MEMORY-SYSTEM-v2): приёмник захват-хуков (hooks/maxgraph_hook.py → /api/ingest) ---
# Хуки хоста (Claude Code/DSH) шлют батчи событий из локального spool'а; тяжёлой
# логики тут нет: событие → дайджест ≤200 символов в пульс (source="hook").
# Идемпотентность:
# content-hash (SHA256 hook+project+ts+data) — последние 1000 в ingest-hashes.json
# (ротация), повторная доставка spool'а → {"dedup": n}, без дублей в пульсе.
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd")
INGEST_MAX_EVENTS = 100       # батч до 100 событий
INGEST_MAX_BODY = 1_000_000   # лимит тела 1MB
INGEST_RATE_LIMIT = 60        # запросов/мин с IP (Ф8 LOW: раньше endpoint был без per-IP лимита)
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
    if hook == "PostToolUse":
        text = (f"{short(data.get('tool'), 40)}: {short(data.get('input'), 100)}"
                f" → {short(data.get('response'), 50)}")
    elif hook == "UserPromptSubmit":
        text = short(data.get("prompt"), 190)
    elif hook == "SessionStart":
        text = f"старт сессии ({short(data.get('source'), 60)})"
    elif hook == "SessionEnd":
        text = f"конец сессии ({short(data.get('reason'), 60)})"
    else:  # Stop
        text = "стоп агента"
    return text[:200]


@mcp.custom_route("/api/ingest", methods=["POST"])
async def api_ingest(request: Request):
    """Приёмник захват-хуков (Ф5). Bearer — middleware TokenAuth (путь не публичный).
    Тело: {"events": [{"hook","project","ts","data"}, …]} — до 100 событий, ≤1MB.
    SessionEnd: data.open_tasks (В2.1, список строк) → открытые task-сущности
    проекта (дедуп по title среди открытых). SessionEnd-сессия + open_tasks всего
    батча пишутся ОДНИМ _load/_save (Ф8).
    Ф8 (LOW): per-IP rate-limit 60/мин (общий in-memory лимитер, TRUST_PROXY-aware
    через _client_ip) — за Bearer клиент с утёкшим/шумным токеном мог флудить
    запись (пульс + файлы). 429 + Retry-After."""
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
    # Ф8 (LOW): полный slug в имени — 4-буквенный префикс коллидировал
    # («alpha»/«alps» → «alph»; сессии разных проектов сливались в одно имя).
    # Ф8 (LOW): ОДИН _load() на весь батч — check-then-create всех SessionEnd-
    # проектов на одном загруженном графе, ОДИН _save в конце (было N×_load +
    # N×_save на N проектов; семантика merge/project-автосоздание/part-of
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
                    _out, ch = _create_entities_graph(ents, rels, [{
                        "name": pname, "title": slug, "entityType": "project",
                        "observations": [],
                    }], project="", source="hook")
                    changed = changed or ch
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
                # В2.1 (work_state): SessionEnd data.open_tasks → task-сущности
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
    return JSONResponse({"ok": True, "accepted": len(fresh), "dedup": dedup})


# ------------------------------------------------------------------ store ----

CODES_FILE = DATA_DIR / "codes.json"
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # без 0/O, 1/I/L
_TIERS = {"pro": {"price": "от 149₽/мес", "label": "Pro"}}  # team убран в v0.13.0: один тариф, лесенка длительности
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
            continue  # v0.33.2: донат никогда не конвертируется в лицензию
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
            os.chmod(CODES_FILE, 0o600)  # license keys открытым текстом (tenant-токены — только sha256, №6) — только owner
        except OSError:
            pass  # Windows/экзотика — best-effort


def _new_code() -> str:
    import secrets

    def group():
        return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))

    return f"MG-{group()}-{group()}-{group()}"


# --- v0.18.0: реестр отозванных ключей + CORS для покупки из дашборда -----------

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


# --- v0.14: учёт установок (анонимный install-ping self-hosted копий) ----------

_installs_lock = threading.Lock()
_INSTALL_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# v0.16.3: серверный якорь триала по machine fingerprint (sha256 hex)
_FP_RE = re.compile(r"^[0-9a-f]{64}$")
# v0.39.0: способ установки в install-ping; неизвестное значение не храним
_INSTALL_FLAVORS = frozenset({"docker", "rancher", "python"})
_INSTALLS_CAP = 10000   # потолок installs-дикта; вытеснение самых старых по last_seen
_TRIALS_CAP = 100_000   # потолок fp-реестра; переполнение → ответ без trial-блока (не 5xx)


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


# --- host-fp: реестр привязок license key → machine fingerprints (2 слота) ----
# Платный ключ живёт максимум на 2 машинах (решение владельца, переноса нет —
# отвязка только владельцем через /admin). Ключи храним как sha256 (как
# revoke-реестр), fp — sha256-хеши машин (сырое железо никогда не уходит).

MACHINES_FILE = DATA_DIR / "machines.json"
_machines_lock = threading.Lock()
_MACHINES_MAX_FP = 1      # 1 ключ = 1 машина (решение владельца 2026-10-13, заменило «2 машины»)
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


def _machine_bind(data: dict, key: str, fp: str, now: float) -> str:
    """Привязать fp к ключу (зовётся под _machines_lock) → "known"|"bound"|"limit".
    known — fp уже привязан; bound — свободный слот был, привязали; limit — слоты заняты.
    Legacy (ключ без единой привязки) принимает первый fp — решение владельца №5."""
    h = _key_hash(key)
    rec = data["machines"].get(h)
    if rec and fp in (rec.get("fps") or []):
        rec["updated"] = now  # свежесть = защита от вытеснения по капу
        return "known"
    if rec is None:
        rec = data["machines"][h] = {"fps": [], "first_seen": now}
    fps = rec.setdefault("fps", [])
    if len(fps) >= _MACHINES_MAX_FP:
        return "limit"
    fps.append(fp)
    rec["updated"] = now
    if len(data["machines"]) > _MACHINES_CAP:  # вытеснение самых старых по updated
        stale = sorted(data["machines"], key=lambda k: data["machines"][k].get("updated", 0))
        for k in stale[: len(data["machines"]) - _MACHINES_CAP]:
            del data["machines"][k]
    return "bound"


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
    (license key в _relay_config_pull) на чужой хост (SECURITY ревью v0.21.0, P1)."""

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
    """GET → распарсенный dict или None (v0.18.2: проверка новой версии на магазине).
    Тело ограничено 64 КБ — скомпрометированный ответ не съест память процесса."""
    raw = urllib.request.urlopen(url, timeout=timeout).read(65536)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _telemetry_ping(post=None, env=None, data_dir=None):
    """Анонимный install-ping на магазин: install_id + версия + install_flavor
    (v0.39.0) + fp (sha256-хеш машины, v0.16.3). Всегда ON (v0.41.0: выключатель
    TELEMETRY=0 удалён решением владельца). Магазин отвечает якорем начала триала:
    started_at РАНЬШЕ локального first_seen → отматываем триал на якорь
    (снос data/ не даёт новые 7 дней); позже/битый ответ — игнор. Best-effort:
    оффлайн/таймаут — молча пропускаем, старт сервера не блокируем (fail-open)."""
    env = env if env is not None else os.environ
    if STORE_MODE:
        return
    payload = {"install_id": _install_id(data_dir), "version": VERSION,
               # v0.39.0: способ установки (доля python-фолбэков). INSTALL_FLAVOR
               # ставит установщик (install.sh/install.ps1); пусто → autodetect.
               "install_flavor": (env.get("INSTALL_FLAVOR") or "").strip()
               or ("docker" if os.path.exists("/.dockerenv") else "python")}
    fp = licensing.machine_fingerprint()
    if fp:
        payload["fp"] = fp
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
    """v0.28.0: install-ping (регистрирует install_id+fp на магазине) → relay-pull.
    Порядок важен для свежей установки: триальный грант принимается магазином
    только для известного fp. Раньше pull шёл только из суточного watchdog'а и
    при активации лицензии — окно «семантика OFF» на минуты после КАЖДОГО
    рестарта (директива владельца: семантический поиск не выключается)."""
    _telemetry_ping()
    _relay_config_pull()


def _relay_config_pull(env=None):
    """v0.19.0: zero-config hosted-фичи — relay-конфиг по license key магазина (embed).

    v0.28.0: нет ключа, но жив триал — триальный грант {install_id, fp}:
    магазин принимает только известную по install-ping пару с неистёкшим
    серверным якорем триала.

    Fail-open, как весь watchdog. Провайдера, заданного явно в env (EMBED_URL),
    не трогаем — свой endpoint осознанный opt-in и важнее relay.
    Токен 7д — суточный прогон держит его свежим.
    """
    env = env if env is not None else os.environ
    if (env.get("EMBED_URL") or "").strip():
        return
    try:
        key = licensing.current_key(DATA_DIR, LICENSE_SECRET)
        if key:
            payload = {"key": key}
        else:
            if licensing.status(DATA_DIR, LICENSE_SECRET).get("state") != "trial":
                return  # expired — без relay (запись и так заблокирована)
            fp = licensing.machine_fingerprint()
            if not fp:
                return
            payload = {"install_id": _install_id(), "fp": fp}
        cfg = _post_json(PUBLIC_BASE_URL + "/api/relay/config", payload, timeout=8)
        ok = isinstance(cfg, dict) and cfg.get("ok")
        if not ok:  # v0.40.0: молчание мешало диагностике «нет семантики» у покупателя
            err = cfg.get("error") if isinstance(cfg, dict) else None
            log.warning("relay: конфиг не получен (%r)", str(err or "магазин недоступен")[:120])
        base = (PUBLIC_BASE_URL or "").rstrip("/")
        # SECURITY v0.19.0: конфиг принимаем только с origin самого магазина по
        # https — иначе скомпрометированный ответ увёл бы relay-токен и поток
        # текстов сущностей (основной + до 4 окон фактов, ≤4200 симв, v0.20.2)
        # на чужой хост.
        emb = ok and cfg.get("embed")
        if isinstance(emb, dict) and emb.get("url") and emb.get("key"):
            url = str(emb["url"])
            if not (base.startswith("https://") and url.startswith(base + "/")):
                log.warning("relay: embed-конфиг с чужого origin отклонён: %r", url[:80])
            else:
                embed.configure_runtime(url=url, key=str(emb["key"]),
                                        model=str(emb.get("model") or "") or None,
                                        query_prefix=emb.get("query_prefix"),
                                        passage_prefix=emb.get("passage_prefix"))
                _ensure_embed_loop()  # цикл мог не стартовать при boot (embed был выключен)
                log.info("relay: embed-конфиг применён (model=%.80r)",
                         emb.get("model") or "default")
        # W3 (v0.27.0): словарь синонимов — независим от embed блок; невалиден/
        # старая версия → rankcfg молча отклоняет, клиент остаётся на last-good
        rank = ok and cfg.get("rank")
        if isinstance(rank, dict) and rankcfg.apply_rank_config(rank, path=RANK_CONFIG_FILE):
            log.info("relay: rank-конфиг применён (version=%s, синонимов=%d)",
                     rank.get("version"), len(rank.get("synonyms") or {}))
    except Exception as e:
        # hosted-фичи опциональны — сбой relay не ломает инстанс, но причину пишем (v0.40.0)
        log.warning("relay: конфиг не получен: %r", e)


def _watchdog_iteration(env):
    """Один прогон watchdog (v0.27.0: вынесен из цикла для тестов и decouple).

    v0.41.0: телеметрия всегда ON (выключатель TELEMETRY=0 удалён) — параметр
    telemetry упразднён; отправка (ревалидация, апдейт-чип) и доставка
    relay-конфигов работают всегда."""
    # v0.18.2: апдейт-чип — узнаём свежую версию даже без ключа (триал тоже обновляется)
    try:
        v = _get_json(PUBLIC_BASE_URL + "/api/version", timeout=8)
        if isinstance(v, dict) and v.get("version"):
            licensing.set_latest(DATA_DIR, LICENSE_SECRET, str(v["version"]))
    except Exception:
        pass  # fail-open: апдейт-чип не критичен, лицензию проверяем ниже
    key = licensing.current_key(DATA_DIR, LICENSE_SECRET)
    if key:
        payload = {"key": key}
        fp = licensing.machine_fingerprint()
        if fp:
            payload["fp"] = fp  # host-fp: магазин учитывает машину ключа (2 слота)
        # v0.38.0: + install_id — магазин связывает ключ с инстансом (self-ref блок рефералки)
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
                _relay_config_pull(env)  # v0.19.0: свежий relay-токен раз в сутки
            elif resp.get("error") == "machine_limit":
                # host-fp, решение №6: machine_limit ≠ «ключ невалиден» — локальный
                # state НЕ затираем (fail-open), лицензия продолжает работать
                log.warning("watchdog: магазин ответил machine_limit — лицензия сохранена")


def _license_watchdog(env=None):
    """v0.18.0: суточная онлайн-ревалидация лицензии на магазине (анти-отзыв).

    Магазин сказал «revoked» → licensing.set_revoked(revoked_at сервера) → grace
    72ч на запись, потом expired; «ok» после unrevoke → отметка снимается.
    Строго fail-open: нет сети/ключа/PUBLIC_BASE_URL, STORE_MODE — спим дальше,
    лицензию не трогаем. Пульс пишем только на переходах.
    v0.41.0: телеметрия всегда ON — выключатель TELEMETRY=0 удалён.
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


def _code_rec(code: str, tier: str, status: str = "new", note: str = "") -> dict:
    return {"code": code, "tier": tier, "status": status, "note": note,
            "createdAt": now_iso(), "soldAt": now_iso() if status == "sold" else None,
            "activatedAt": None, "tenant": None}


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
    """sha256 токена кабинета: файл хранит хеш, сам токен показывается клиенту один раз."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_tenant(key: str):
    """Тенант по токену кабинета (Bearer): sha256(key) == token_hash. Tenant dict или None.

    Легаси-записи с plaintext-полем `token` — lazy-миграция: при первой успешной
    верификации пишем token_hash и удаляем plaintext (перечитываем под _codes_lock,
    чтобы не затереть конкурентную запись).
    """
    if not key:
        return None
    import secrets
    data = _load_codes()
    h = _token_hash(key)
    t = next((x for x in data["tenants"] if x.get("token_hash") == h), None)
    if t:
        return t
    kb = key.encode("utf-8", "ignore")
    t = next((x for x in data["tenants"]
              if secrets.compare_digest(str(x.get("token") or "").encode("utf-8", "ignore"), kb)), None)
    if not t:
        return None
    with _codes_lock:
        data = _load_codes()
        t2 = next((x for x in data["tenants"] if x.get("id") == t.get("id")), None)
        if t2 and not t2.get("token_hash") and t2.get("token") == key:
            t2["token_hash"] = h
            del t2["token"]
            _save_codes(data)
    return t


def migrate_token_hashes() -> int:
    """Одноразовая миграция: легаси-тенанты с plaintext `token` → `token_hash` (№6).

    Вызывается при старте сервера (plaintext известен из файла); страховка для
    записей, всплывших позже — lazy-upgrade внутри verify_tenant. Возвращает число
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
    (SECURITY: публичные эндпоинты, memory-DoS через chunked). Fast-path по заголовку
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
    Запятая/скобки/кавычки (v0.32.0): «a@b.c,evil@x» иначе проходило → smtplib
    send_message слал письмо с ключом ОБОИМ адресам (getaddresses по To)."""
    e = str(value or "")[:254].strip().lower()
    if any(c in e for c in "\r\n\t,<>'\""):
        return None
    return e if e and "@" in e and " " not in e else None


def _gen_codes(count: int, tier: str, note: str = "") -> list:
    with _codes_lock:
        data = _load_codes()
        existing = {c["code"] for c in data["codes"]}
        out = []
        while len(out) < count:
            code = _new_code()
            if code in existing:
                continue
            rec = _code_rec(code, tier, "new", note)
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
    return await _landing_page(request)


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
    # v0.37.0: атрибуция рефералки — ?ref=XXXX-XXXX → кука mg_ref (30д, Lax) +
    # счётчик переходов. Только известные коды (мусор не засоряет метрики);
    # buybox дальше шлёт код в /billing/create, скидка считается серверно.
    code = _ref_norm(request.query_params.get("ref"))
    if code:
        with _ref_lock:
            refs = _load_referrals()
            rec = refs.get(code) if refs is not None else None
            if rec is not None:
                # first-touch (L1) + анти-накрутка (L2): у гостя уже стоит валидная
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
    # v0.18.0: connect-src + магазин — диалог покупки ходит на rezon-ai.ru
    # (/api/plans, /api/billing/*) прямо из браузера.
    if STORE_MODE:
        return PlainTextResponse("Not Found", status_code=404)
    return HTMLResponse(_page_html("dashboard.html"), headers=_NOCACHE_DASH)


@mcp.custom_route("/cabinet", methods=["GET"])
async def cabinet_page(request: Request):
    # Кабинет покупателя ОТМЕНЁН (решение владельца 2026-10-11): mg_-ключ вводится
    # локально в дашборде, магазин только verify. На STORE_MODE — 404 (как /app):
    # данных покупателя на магазине в браузере быть не должно. У self-hosted
    # инстансов страница остаётся (своя локальная копия).
    if STORE_MODE:
        return PlainTextResponse("Not Found", status_code=404)
    return HTMLResponse(_page_html("cabinet.html"), headers=_NOCACHE)


@mcp.custom_route("/admin", methods=["GET"])
async def admin_page(request: Request):
    """Кабинет владельца магазина: статистика, ключи, учёт. ТОЛЬКО на инстансе магазина
    (STORE_MODE=1). Страница — из репо (app/admin.html); с v0.41.0 admin.html
    версионируется, DATA_DIR-оверрайд убран (шэдоуил новую версию на проде).
    Self-hosted инстансы → 404.
    Оболочка публичная (gate-форма токена), данные — /api/* по Bearer."""
    if not STORE_MODE:
        return PlainTextResponse("Not Found", status_code=404)
    return HTMLResponse(_page_html("admin.html"), headers=_NOCACHE)


def _store_gate():
    """v0.18.3: служебные эндпоинты учёта (покупки/активации/коды/установки, вход
    владельца, платёжный контур) существуют ТОЛЬКО на инстансе магазина. Self-hosted
    копии покупателей отвечают 404 — они не должны ни иметь доступа, ни знать о
    существовании учёта. PlainText — неотличимо от 404 роутера (LOW: вкус ответа)."""
    return None if STORE_MODE else PlainTextResponse("Not Found", status_code=404)


_OG_FILE = Path(__file__).with_name("og.png")  # social-preview, копируется в образ Dockerfile'ом


_FONTS_DIR = Path(__file__).with_name("static") / "fonts"
_FONT_RE = re.compile(r"[a-z0-9-]+\.woff2")


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


_DEMO_SHOT_LOCAL = Path(__file__).with_name("static") / "demo-dashboard-dark.png"
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
    return JSONResponse({"created": _gen_codes(count, tier, body.get("note", ""))}, headers=_NOCACHE)


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
    """Анонимный install-ping self-hosted копий (v0.14; v0.41.0 — всегда ON,
    оптаута TELEMETRY=0 больше нет).
    install_id + версия + install_flavor (v0.39.0) + опциональный fp (sha256-хеш
    машины, v0.16.3) — ни email, ни контента памяти, ни IP-логирования.
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
    flavor = str(body.get("install_flavor") or "")[:16]  # v0.39.0
    now = time.time()
    trial_ts = None
    with _installs_lock:
        data = _load_installs()
        if data is None:  # битый installs.json — fail-open без записи (не затираем)
            return JSONResponse({"ok": True}, headers=_NOCACHE)
        rec = data["installs"].get(iid)
        first = rec is None
        rec = rec or {"first_seen": now, "starts": 0}
        rec.update({"last_seen": now, "version": ver, "starts": int(rec.get("starts") or 0) + 1})
        if flavor in _INSTALL_FLAVORS:  # вайтлист: неизвестное молча не храним (forward-compat)
            rec["install_flavor"] = flavor  # перезаписывается при миграции python → docker
        if fp:
            rec["fp"] = fp  # fp установки — для админки
        data["installs"][iid] = rec
        if len(data["installs"]) > _INSTALLS_CAP:  # вытеснение самых старых по last_seen
            stale = sorted(data["installs"], key=lambda k: data["installs"][k].get("last_seen", 0))
            for k in stale[: len(data["installs"]) - _INSTALLS_CAP]:
                del data["installs"][k]
        if fp:
            trials = data.setdefault("trials", {})
            t = trials.get(fp)
            if t is None:
                if len(trials) < _TRIALS_CAP:
                    t = trials[fp] = {"started_at": now, "reinstalls": 0, "installs": [iid]}
            else:
                seen = t.setdefault("installs", [])
                if iid not in seen:  # новый install_id для этого fp = переустановка
                    t["reinstalls"] = int(t.get("reinstalls") or 0) + 1
                    if len(seen) < 100:  # служебный перечень — без роста в бесконечность
                        seen.append(iid)
            if t is not None:
                trial_ts = t["started_at"]  # якорь НИКОГДА не двигается вперёд
        _save_installs(data)
    if first and fp:
        # событие только для fp-анкоренных установок — анонимный флуд
        # uuid'ами не засоряет таймлайн админки (fp флудеру вычислять лень)
        _event("install_seen", iid, ver)
    out = {"ok": True}
    if trial_ts is not None:
        out["trial"] = {"started_at": trial_ts}
        out["trial_days"] = licensing.trial_days()
    return JSONResponse(out, headers=_NOCACHE)


# --- W2 v0.26.0: learn-events (SPEC-learn-events.md, issue #21) ---
_learn_lock = threading.Lock()
_learn_prune_day = ""        # UTC-дата последнего retention-прогона
_LEARN_RETENTION_SEC = 90 * 86400
_LEARN_CAP = 500_000         # belt&braces: drop oldest при prune
_LEARN_BODY_CAP = 16384
_LEARN_MAX_EVENTS = 50
_LEARN_TEXT_MAX = 200
_LEARN_BYTES_CAP = 32 * 1024 * 1024  # stat-клапан до append: между
                                     # суточными prune файл не растёт безмерно


def _learn_clean(text):
    """Серверная копия санитизации клиента: границе доверия не верим —
    lone surrogate легален в JSON, но ломает utf-8 encode при записи."""
    return (text.encode("utf-8", "ignore").decode("utf-8")
            .replace("\x00", "").strip()[:_LEARN_TEXT_MAX])


def _learn_prune(now):
    """Retention 90д: переписать файл без старых/битых строк + кап (drop oldest).
    Зовётся под _learn_lock раз в UTC-день при первой записи дня.
    shortcut: full-parse синхронно в event loop; при капе 32МБ это ~секунда
    раз в сутки — если магазин вырастет, вынести в поток."""
    global _learn_prune_day
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    if day == _learn_prune_day or not LEARN_EVENTS_FILE.exists():
        _learn_prune_day = day
        return
    cutoff = now - _LEARN_RETENTION_SEC
    kept = []
    try:
        lines = LEARN_EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError, ValueError):
        lines = []  # битый/нечитаемый файл → начинаем с чистого
    for line in lines:
        try:
            ts = float(json.loads(line).get("ts") or 0)
        except Exception:
            continue  # битая строка выкидывается при prune
        if ts >= cutoff:
            kept.append(line)
    kept = kept[-_LEARN_CAP:]
    try:  # атомарно: tmp + replace — крэш/disk-full не затирает файл
        tmp = LEARN_EVENTS_FILE.with_suffix(".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, LEARN_EVENTS_FILE)
    except OSError:
        pass  # read-only data/ — retention пропустим, запись ниже откажет честно
    _learn_prune_day = day


@mcp.custom_route("/api/telemetry/learn", methods=["POST"])
async def api_telemetry_learn(request: Request):
    """Анонимные learn-events «запрос → открытый заголовок» (W2; v0.41.0 — без
    оптаута, телеметрия всегда ON). Без license key/install_id/fp/IP — только
    тексты запроса (≤200) и заголовок (≤200) + счётчик. ts ставит СЕРВЕР (часам
    клиента не верим). Хранение: learn_events.jsonl, retention 90 дней.
    Пер-событийная валидация: битое событие пропускаем, весь пакет битый → 400."""
    ip = _client_ip(request)
    if _rate_limited(ip, "learn", 30, 3600):
        return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
    body, err = await _read_capped(request, _LEARN_BODY_CAP)
    if err is not None:
        return err
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list) or not events or len(events) > _LEARN_MAX_EVENTS:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    now = time.time()
    accepted = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        q, t, c = ev.get("q"), ev.get("t"), ev.get("c", 1)
        if not isinstance(q, str) or not isinstance(t, str):
            continue
        q = _learn_clean(q) if isinstance(q, str) else ""
        t = _learn_clean(t) if isinstance(t, str) else ""
        if not q or not t:
            continue
        if isinstance(c, bool) or not isinstance(c, int) or not (1 <= c <= 1000):
            continue
        accepted.append({"ts": now, "q": q, "t": t, "c": c})
    if not accepted:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    with _learn_lock:
        _learn_prune(now)
        try:  # stat-клапан ДО append — файл не растёт сверх капа
            full = (LEARN_EVENTS_FILE.exists()
                    and LEARN_EVENTS_FILE.stat().st_size > _LEARN_BYTES_CAP)
        except OSError:
            full = False
        if full:
            return JSONResponse({"ok": False, "error": "store_full"},
                                status_code=503)  # клиент сохранит буфер
        try:
            with LEARN_EVENTS_FILE.open("a", encoding="utf-8") as f:
                for ev in accepted:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            # read-only data/ → честный отказ; клиент сохранит буфер (fail-open)
            return JSONResponse({"ok": False, "error": "store_unavailable"},
                                status_code=503)
    return JSONResponse({"ok": True, "accepted": len(accepted)}, headers=_NOCACHE)


# --- W3 v0.27.0: агрегация словаря синонимов из learn-events (SPEC-rank-config) ---
RANK_VETO_FILE = DATA_DIR / "rank_veto.json"   # [[stem_a, stem_b], ...] — вето владельца
_rank_lock = threading.Lock()
_rank_cache = {"computed_at": 0.0, "mtime": -1.0,
               "cfg": {"version": 0, "synonyms": {}}, "stats": {}}
_RANK_RECOMPUTE_SEC = 3600   # пересчёт не чаще раза в час, даже если mtime пляшет
_RANK_MIN_COUNT = 5          # авто-включение: суммарный c ≥ 5
_RANK_MIN_DAYS = 3           # …в ≥3 разных UTC-дня (прокси «разных пользователей»)
_RANK_NOISE_MIN = 2          # count < 2 не храним (одноразовый шум)
_RANK_PAIRS_CAP = 500
_RANK_SYNONYMS_CAP = 5       # синонимов на токен (топ по count)
_RANK_STATS_CAP = 200_000    # кап пар ДО фильтра шума (пара
                             # из 100-токенных q/t плодит ~10k записей за 450 байт)


def _load_rank_veto():
    """Вето-список владельца. Битый файл → пусто (не блокируем агрегацию)."""
    try:
        v = json.loads(RANK_VETO_FILE.read_text(encoding="utf-8"))
        return {tuple(p) for p in v
                if isinstance(p, list) and len(p) == 2
                and all(isinstance(x, str) for x in p)}
    except Exception:
        return set()


def _surface_map(text):
    """стем → Counter поверхностных форм (значение словаря должно быть читаемым
    в /admin; re-tokenize формы вернёт тот же стем)."""
    out = {}
    for _, _, raw in searchrank._subtokens(str(text or "")):
        out.setdefault(searchrank._stem(raw), {})
        out[searchrank._stem(raw)][raw] = out[searchrank._stem(raw)].get(raw, 0) + 1
    return out


def _rank_config_compute(now):
    """learn_events.jsonl → (cfg {version, synonyms}, stats для /admin).

    Кандидат: стем запроса вне заголовка → стем заголовка вне запроса; все
    комбинации получают +c — истинные пары накапливаются быстрее шума.
    Активен при count≥5 и days≥3 и не в вето. version = int(mtime событий)."""
    try:
        mtime = LEARN_EVENTS_FILE.stat().st_mtime
    except OSError:
        return {"version": 0, "synonyms": {}}, {}
    # вето должно доезжать до клиентов немедленно — версия
    # учитывает и mtime вето-файла, иначе клиент отклонит пересчёт как «старый»
    try:
        vmtime = RANK_VETO_FILE.stat().st_mtime
    except OSError:
        vmtime = 0.0
    version = int(max(mtime, vmtime) * 1000)  # мс-разрешение: вето в ту же
                                              # секунду всё равно бампит версию
    # монотонность: mtime квантуется (NTFS ~15.6мс) — вето в пределах кванта
    # давало ТУ ЖЕ версию, клиент отклонял пересчёт как «старый»
    # (флаки test_version_bumps_on_veto на быстрых машинах)
    prev = _rank_cache["cfg"].get("version") or 0
    if version <= prev:
        version = prev + 1
    stats = {}  # (qs, ts) -> {count, days:set, surf_q:Counter, surf_t:Counter}
    try:
        lines = LEARN_EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError, ValueError):
        lines = []
    for line in lines:
        try:
            ev = json.loads(line)
            q, t = ev["q"], ev["t"]
        except Exception:
            continue  # битая строка — не наша забота (prune выкинет)
        c = ev.get("c", 1)
        if isinstance(c, bool) or not isinstance(c, int) or not (1 <= c <= 1000):
            c = 1
        qs_map, ts_map = _surface_map(q), _surface_map(t)
        only_q = set(qs_map) - set(ts_map)
        only_t = set(ts_map) - set(qs_map)
        if not only_q or not only_t:
            continue
        try:
            day = time.strftime("%Y-%m-%d", time.gmtime(float(ev.get("ts") or 0)))
        except (TypeError, ValueError, OSError):
            continue
        for a in only_q:
            for b in only_t:
                key_ab = (a, b)
                st = stats.get(key_ab)
                if st is None:
                    if len(stats) >= _RANK_STATS_CAP:
                        continue  # кап против DoS-раздувания
                    st = stats[key_ab] = {"count": 0, "days": set(),
                                          "surf_q": {}, "surf_t": {}}
                st["count"] += c
                st["days"].add(day)
                for raw, n in qs_map[a].items():
                    st["surf_q"][raw] = st["surf_q"].get(raw, 0) + n
                for raw, n in ts_map[b].items():
                    st["surf_t"][raw] = st["surf_t"].get(raw, 0) + n
    stats = {k: v for k, v in stats.items() if v["count"] >= _RANK_NOISE_MIN}
    veto = _load_rank_veto()
    active = [(k, v) for k, v in stats.items()
              if v["count"] >= _RANK_MIN_COUNT and len(v["days"]) >= _RANK_MIN_DAYS
              and k not in veto]
    active.sort(key=lambda kv: -kv[1]["count"])
    by_q = {}
    for (a, b), st in active[:_RANK_PAIRS_CAP]:
        if len(by_q.setdefault(a, [])) < _RANK_SYNONYMS_CAP:
            by_q[a].append((b, st))
    synonyms = {a: [max(v["surf_t"].items(), key=lambda kv: kv[1])[0]  # частая форма
                    for _, v in pairs]
                for a, pairs in by_q.items()}
    return {"version": version, "synonyms": synonyms}, stats


def _rank_config_cached():
    """cfg из кэша; пересчёт при смене mtime learn_events.jsonl, не чаще часа.
    Вето-эндпоинт сбрасывает кэш принудительно (mtime=-1)."""
    now = time.time()
    with _rank_lock:
        try:
            mtime = LEARN_EVENTS_FILE.stat().st_mtime
        except OSError:
            mtime = 0.0
        c = _rank_cache
        if mtime and (mtime != c["mtime"] or c["mtime"] < 0) \
                and now - c["computed_at"] >= _RANK_RECOMPUTE_SEC:
            c["cfg"], c["stats"] = _rank_config_compute(now)
            c["mtime"] = mtime
            c["computed_at"] = now
        return c["cfg"]


def _rank_admin_view():
    """Витрина /admin: активные/кандидаты/заветоенные пары с читаемыми формами."""
    _rank_config_cached()  # кэш свежий в пределах часового гейта
    with _rank_lock:
        stats = dict(_rank_cache["stats"])
    veto = _load_rank_veto()
    active, candidates, vetoed = [], [], []
    for (a, b), st in sorted(stats.items(), key=lambda kv: -kv[1]["count"]):
        row = {"a": a, "b": b,
               "a_surf": max(st["surf_q"], key=st["surf_q"].get) if st["surf_q"] else a,
               "b_surf": max(st["surf_t"], key=st["surf_t"].get) if st["surf_t"] else b,
               "count": st["count"], "days": len(st["days"])}
        if (a, b) in veto:
            vetoed.append(row)
        elif st["count"] >= _RANK_MIN_COUNT and len(st["days"]) >= _RANK_MIN_DAYS:
            active.append(row)
        else:
            candidates.append(row)
    return {"active": active[:200], "candidates": candidates[:200],
            "vetoed": vetoed[:200],
            "thresholds": {"count": _RANK_MIN_COUNT, "days": _RANK_MIN_DAYS}}


@mcp.custom_route("/api/admin/synonyms", methods=["GET"])
async def api_admin_synonyms(request: Request):
    """W3 (v0.27.0): витрина словаря синонимов для владельца магазина."""
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    return JSONResponse(_rank_admin_view(), headers=_NOCACHE)


@mcp.custom_route("/api/admin/synonyms/veto", methods=["POST"])
async def api_admin_synonyms_veto(request: Request):
    """W3 (v0.27.0): вето пары (a,b) — исключается из словаря навсегда
    (снятие вето — ручной edit data/rank_veto.json). Кэш сбрасывается сразу."""
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    body, err = await _read_capped(request, 4096)
    if err is not None:
        return err
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    a = str(body.get("a") or "").strip() if isinstance(body, dict) else ""
    b = str(body.get("b") or "").strip() if isinstance(body, dict) else ""
    if (not a or not b or len(a) > 64 or len(b) > 64
            or any(ch.isspace() for ch in a + b)):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    try:
        (a + b).encode("utf-8")  # LOW (W3): lone surrogate легален в JSON, но
    except UnicodeEncodeError:   # ломает запись veto-файла → 400, не 500
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    with _rank_lock:
        try:
            veto = json.loads(RANK_VETO_FILE.read_text(encoding="utf-8"))
            if not isinstance(veto, list):
                veto = []
        except Exception:
            veto = []
        if [a, b] not in veto:
            veto.append([a, b])
            tmp = RANK_VETO_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(veto, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, RANK_VETO_FILE)
        _rank_cache["mtime"] = -1.0      # форс-пересчёт при следующем чтении
        _rank_cache["computed_at"] = 0.0
    return JSONResponse({"ok": True}, headers=_NOCACHE)


@mcp.custom_route("/api/installs", methods=["GET"])
async def api_installs_list(request: Request):
    """Машины для админки (v0.41.0, заменило плоский список установок):
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
    trials = data.get("trials") or {}
    fp2kh = {}
    try:  # fp → key_hash из machines.json (лицензионные привязки)
        for kh, rec in (_load_machines().get("machines") or {}).items():
            for fp in rec.get("fps") or []:
                fp2kh[fp] = kh
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
    for iid, rec in installs.items():
        fp = rec.get("fp")
        if fp:
            by_fp.setdefault(fp, []).append({"install_id": iid, **rec})
        else:
            unknown.append({"install_id": iid, **rec})
    machines = []
    for fp in set(by_fp) | set(trials) | set(fp2kh):
        t = trials.get(fp) or {}
        all_rows = by_fp.get(fp, [])
        last = max([r.get("last_seen", 0) for r in all_rows] or [0])
        rows = sorted(all_rows,
                      key=lambda r: r.get("last_seen", 0), reverse=True)[:50]  # кап на машину
        machines.append({"fp": fp,
                         "started_at": t.get("started_at"),
                         "reinstalls": _safe_int(t.get("reinstalls")),
                         "installs": rows,
                         "license": kh2lic.get(fp2kh.get(fp) or ""),
                         "last_seen": last})
    machines.sort(key=lambda m: m["last_seen"], reverse=True)
    unknown.sort(key=lambda r: r.get("last_seen", 0), reverse=True)
    return JSONResponse({"machines": machines[:500],
                         "unknown_installs": unknown[:500],
                         "kpi": {"machines": len(machines), "installs": len(installs)}},
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
    return JSONResponse({"payments": out[:500]}, headers=_NOCACHE)


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
            # v0.22.3: восстановление = ротация КЛЮЧА АКТИВАЦИИ (раньше ротировался токен
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
                # ручной код (/api/codes/generate): лицензии нет — выпускаем бессрочную
                # (v0.22.3; решение владельца — убить всегда можно через revoke)
                lic = licensing.generate_key(LICENSE_SECRET, email or "buyer@unknown",
                                             rec["tier"], 0)
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
            log.error("recovery email failed for tenant %s: %s", r_tid, e)  # без email в логах (PII)
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
    _event("code_activated", tenant_id, f'{rec["tier"]} · {code}')
    return JSONResponse({"ok": True, "tier": rec["tier"], "token": token, "licenseKey": lic,
                         "cabinet": f"/cabinet?key={token}"}, headers=_NOCACHE)


# --- cloud trial (v0.8): email → кабинет, без оплаты. License key НЕ выдаётся ---
# (self-hosted триал 7 дней — отдельно, см. licensing.py). Абьюз-лимиты:
# 2/сутки с IP + глобальный потолок 500 trial-кодов. Email обязателен: кабинет
# привязан к почте; повторная регистрация на тот же email → 409 (вход по
# email-ссылке — после настройки SMTP-релея).
_TRIAL_MAX_TOTAL = 500


@mcp.custom_route("/api/codes/trial", methods=["POST"])
async def api_codes_trial(request: Request):
    import secrets
    ip = _client_ip(request)
    if _rate_limited(ip, "trial", 2, 86400):
        return JSONResponse({"ok": False, "error": "trial_limit_reached"}, status_code=429)
    try:
        raw, _err = await _read_capped(request, 4096)  # кап по факту и тут
        body = json.loads(raw) if raw and _err is None else {}
    except Exception:
        body = {}
    email = _valid_email(body.get("email") if isinstance(body, dict) else None)
    if not email:
        return JSONResponse({"ok": False, "error": "valid_email_required"}, status_code=400)
    # per-email throttle: ответ email_exists — оракул «зарегистрирован ли этот email»;
    # 1 запрос/час на email делает массовый перебор непрактичным
    if _rate_limited(f"em:{email}", "trial_email", 1, 3600):
        return JSONResponse({"ok": False, "error": "trial_limit_reached"}, status_code=429)
    with _codes_lock:
        data = _load_codes()
        if any(t.get("email") == email for t in data["tenants"]):
            return JSONResponse({"ok": False, "error": "email_exists"}, status_code=409)
        trials = sum(1 for c in data["codes"] if (c.get("note") or "").startswith("trial"))
        if trials >= _TRIAL_MAX_TOTAL:
            return JSONResponse({"ok": False, "error": "trial_exhausted"}, status_code=503)
        existing = {c["code"] for c in data["codes"]}
        code = _new_code()
        while code in existing:
            code = _new_code()
        tenant_id = "t-" + secrets.token_hex(4)
        token = "mg_" + secrets.token_hex(20)
        rec = _code_rec(code, "pro", "activated", "trial")
        rec["activatedAt"] = now_iso()
        rec["tenant"] = tenant_id
        data["codes"].append(rec)
        data["tenants"].append({"id": tenant_id, "token_hash": _token_hash(token), "tier": "pro",
                                "createdAt": now_iso(), "code": code, "email": email})
        _save_codes(data)
    _event("trial_activated", tenant_id, f"{code} · {email}")
    return JSONResponse({"ok": True, "code": code, "token": token,
                         "cabinet": f"/cabinet?key={token}"}, headers=_NOCACHE)


@mcp.custom_route("/api/cabinet", methods=["GET"])
async def api_cabinet(request: Request):
    ip = _client_ip(request)
    if _rate_limited(ip, "cabinet", 30, 60):
        return JSONResponse({"error": "too_many_attempts"}, status_code=429)
    auth = request.headers.get("authorization", "")
    key = auth.removeprefix("Bearer ").strip()[:128]  # ?key= удалён (v0.12.2): токен не должен жить в access-логах; кабинет шлёт Bearer (cabinet.html)
    if not key:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    tenant = verify_tenant(key)
    if not tenant:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # Ленивый бэкфилл: кабинеты, активированные до привязки licenseKey к тенанту
    # (digiseller-коды несли лицензию в записи кода, в тенант не копировалась)
    if not tenant.get("licenseKey") and tenant.get("code"):
        with _codes_lock:
            data = _load_codes()
            t = next((x for x in data["tenants"] if x.get("id") == tenant["id"]), None)
            lic, rec = _license_for_code(data, tenant["code"])
            if t is not None and lic:
                t["licenseKey"] = lic
                t.setdefault("paidAt", (rec or {}).get("activatedAt") or now_iso())
                _save_codes(data)
                tenant = t
    paid_until = 0  # срок подписки: exp из license key (0 = нет/бессрочно)
    if tenant.get("licenseKey"):
        payload, _err = licensing.verify_key(LICENSE_SECRET, tenant["licenseKey"])
        paid_until = int((payload or {}).get("exp") or 0)
    return JSONResponse({"id": tenant["id"], "tier": tenant["tier"],
                         "tierLabel": _TIERS.get(tenant["tier"], {}).get("label", tenant["tier"]),
                         "createdAt": tenant["createdAt"], "code": tenant["code"],
                         "email": tenant.get("email"),
                         "paid": bool(tenant.get("licenseKey")),
                         "licenseKey": tenant.get("licenseKey"),
                         "paidAt": tenant.get("paidAt"),
                         "paidUntil": paid_until}, headers=_NOCACHE)


# --- v0.37.0: реферальная система (SPEC-referral.md) -------------------------
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
    rrec["history"] = rrec["history"][-200:]  # L4: кап истории
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
        rrec["history"] = rrec["history"][-200:]  # L4
        _save_referrals(refs)


@mcp.custom_route("/api/referral/code", methods=["POST"])
async def api_referral_code(request: Request):
    """Магазин: реф-код инстанса → {code, link, clicks, buys, stack_n}.

    Публичный, rate-limit. install_id обязан быть известен по install-ping;
    fp сверяется, если установка его присылала (v0.16.3+). Код детерминирован —
    запись в referrals.json создаётся лениво при первом запросе (lazy-register).
    M2: install_id+fp — ТОЛЬКО в теле POST: query оседает в access-логах, а пара
    install_id+fp = креды к стеку (как ?token= в v0.11)."""
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
            # M3: fp легаси-инсталла не дозаписываем из чужого вызова — иначе первый
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
    """Инстанс: своя реф-карточка для дашборда («Система» → «Пригласи друга», v0.37.0).
    Прокси на магазин /api/referral/code с install_id+fp этого инстанса. Bearer —
    middleware (путь не публичный). Магазин недоступен → {ok:false} — блок скрыт,
    инстанс не ломается (fail-open, как весь hosted-контур)."""
    if STORE_MODE:
        return PlainTextResponse("Not Found", status_code=404)  # как /app: у магазина нет дашборда
    import asyncio
    iid = _install_id()
    fp = licensing.machine_fingerprint() or ""
    out = {"ok": False, "install_id": iid, "fp": fp}
    try:
        # M2: install_id/fp — в теле POST (query оседает в access-логах магазина)
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
    """Публичный прайс для диалога покупки в дашборде (v0.18.0). Секретов нет → CORS."""
    plans = [{"id": pid, "label": PLANS[pid]["label"], "rub": PLANS[pid]["RUB"],
              "usd": PLANS[pid]["USD"], "days": PLANS[pid]["days"]}
             for pid in ("pro1", "pro3", "pro6", "pro12") if pid in PLANS]
    return JSONResponse({"plans": plans}, headers={**_NOCACHE, **_CORS})


@mcp.custom_route("/api/plans", methods=["OPTIONS"])
@mcp.custom_route("/api/billing/pay", methods=["OPTIONS"])
@mcp.custom_route("/api/billing/status", methods=["OPTIONS"])
async def billing_cors_preflight(request: Request):
    """Preflight для кросс-ориджин покупки из дашборда (v0.18.0)."""
    return Response(status_code=204, headers=_CORS)


@mcp.custom_route("/api/billing/pay", methods=["POST"])
async def api_billing_pay(request: Request):
    """Кнопка «Купить» (лендинг/дашборд v0.18.0) → ссылка на оплату Platega.

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
    # v0.38.0: рефералка. body.ref — промокод (кука лендинга mg_ref или ручной ввод);
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
    # (формула владельца: стек включает базу). install_id валидируем мягко по
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
            # M3: стек только при ПОДТВЕРЖДЁННОМ fp — легаси-инсталл без fp стек не применяет
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
        # L6: сырой ответ Platega наружу не отдаём (внутрянка) — детали в серверном логе
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
    licenseKey — дашборд v0.18.0 активирует его сам (автоцикл покупки).
    ref — в POST-теле (query string оседает в access-логах, SECURITY v0.18);
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
    """Публично: текущая версия этого инстанса (магазин отвечает своей — апдейт-чип, v0.18.2).

    Осознанный tradeoff (security-ревью L3): точная версия публична → фингерпринтинг
    для будущих CVE. Принято: магазину публичность нужна, секретов тут нет.
    """
    return JSONResponse({"version": VERSION}, headers=_NOCACHE)


@mcp.custom_route("/api/update/trigger", methods=["POST"])
async def api_update_trigger(request: Request):
    """Кнопка «обновить сейчас» в дашборде (v0.35.0): роняет пустой маркер
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
    """Отдать файл релиза текущей VERSION из RELEASES_DIR (магазин, v0.20.0).
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
    # FileResponse — стриминг (M5 ревью: файл целиком в RAM в async-хендлере —
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
    """Публично: tarball последнего релиза для автообновлений (v0.20.0). Канал
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
    """Публично: ed25519-подпись tarball'а (v0.20.0). Trust anchor — публичный ключ,
    вшитый в образ updater'а при установке: компрометация раздачи (nginx/data) без
    ключа подписи (хранится только на сервере магазина) не даёт подсунуть код клиентам."""
    guard = _release_guard(request)
    if guard is not None:
        return guard
    return _release_file("sig")


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
    try:
        await asyncio.to_thread(_relay_config_pull)  # v0.19.0: семантика сразу после активации
    except Exception:
        pass  # fail-open: активация уже прошла
    return JSONResponse({"ok": True, "email": res.get("email"), "tier": res.get("tier")}, headers=_NOCACHE)


def _activate_key(key: str) -> dict:
    """Активация: локально, а если подпись чужая — онлайн-валидация на сервере магазина.

    Ключи магазина подписаны серверным LICENSE_HMAC_SECRET — self-hosted установка
    локально их не проверит (свой секрет). Тогда: POST {PUBLIC_BASE_URL}/api/license/validate
    → сервер подтверждает → локальный grant. Блокирующий сетевой вызов — вызывать в thread.

    host-fp: с ключом передаём fp машины (магазин: 2 слота на ключ). Ответ
    403 machine_limit — НЕ «ключ невалиден» (решение владельца №6): локальный
    state не трогаем, наружу отдаём machine_limit=True + понятный message.
    """
    res = licensing.activate(DATA_DIR, LICENSE_SECRET, key)
    if res.get("ok") or "signature" not in str(res.get("error", "")):
        return res
    if not PUBLIC_BASE_URL:
        return res
    payload = {"key": key}
    fp = licensing.machine_fingerprint()
    if fp:
        payload["fp"] = fp
    payload["install_id"] = _install_id()  # v0.38.0: связка ключ↔инстанс (self-ref блок рефералки)
    try:
        j = _post_json(f"{PUBLIC_BASE_URL}/api/license/validate", payload, timeout=6)
    except Exception as e:
        j = _http_error_json(e)  # штатный отказ с телом (403 machine_limit)
        if j is None:
            return {"ok": False, "error": f"ключ магазина требует онлайн-активации, сервер недоступен: {e}"}
    if not isinstance(j, dict):
        return {"ok": False, "error": "сервер магазина ответил не-JSON — активация отклонена"}
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
    rec = _is_revoked(_load_codes(), key)  # v0.18.0: реестр отзывов (чарджбэк/фрод)
    if rec:
        _event("license_validate_revoked", payload.get("email") or "", payload.get("tier", ""))
        return JSONResponse({"ok": False, "revoked": True, "error": "license revoked",
                             "revoked_at": rec.get("ts") or 0}, headers=_NOCACHE)
    fp = str(body.get("fp") or "")[:128].strip().lower()
    if fp:
        if not _FP_RE.match(fp):
            return JSONResponse({"ok": False, "error": "bad fp"}, status_code=400)
        try:
            with _machines_lock:
                mdata = _load_machines()
                verdict = _machine_bind(mdata, key, fp, time.time())
                if verdict != "limit":
                    _save_machines(mdata)
        except Exception:  # битый machines.json → fail-closed (500), привязки целы
            log.error("validate: machines.json не читается — отказ (fail-closed)")
            return JSONResponse({"ok": False, "error": "store registry error"}, status_code=500)
        if verdict == "limit":
            _event("license_machine_limit", payload.get("email") or "", fp[:12])
            log.info("validate: machine_limit для %s (fp=%s…)",
                     payload.get("email") or "?", fp[:8])
            return JSONResponse({"ok": False, "error": "machine_limit",
                                 "message": _MACHINE_LIMIT_MSG},
                                status_code=403, headers=_NOCACHE)
        if verdict == "bound":
            _event("license_machine_bound", payload.get("email") or "", fp[:12])
    # v0.38.0: инстанс присылает install_id → связка ключ↔инстанс (key_installs в
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
                            rrec["history"] = rrec["history"][-200:]  # L4
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


_RELAY_EMBED_MODEL = os.environ.get("RELAY_EMBED_MODEL", "intfloat/multilingual-e5-small")


def _relay_pfx(name: str, default: str) -> str:
    """RELAY_EMBED_*_PREFIX из env: валидируем по клиентскому правилу embed._pfx
    (иначе мусор уехал бы всем клиентам, которые его молча
    дропнут → рассинхрон конвенции без единой ошибки). Невалидное → дефолт."""
    raw = os.environ.get(name)
    v = embed._pfx(raw)
    if raw and not v:
        log.warning("%s невалиден (>32/не-printable/не-UTF-8) — дефолт %r",
                    name, default)
    return v or default


# E5-конвенция текстов (prefix-migration): клиент применяет к запросу/окнам.
_RELAY_EMBED_QUERY_PREFIX = _relay_pfx("RELAY_EMBED_QUERY_PREFIX", "query: ")
_RELAY_EMBED_PASSAGE_PREFIX = _relay_pfx("RELAY_EMBED_PASSAGE_PREFIX", "passage: ")


@mcp.custom_route("/api/relay/config", methods=["POST"])
async def api_relay_config(request: Request):
    """Zero-config hosted-фичи (семантический поиск):
    {key} → {ok, embed:{url,key,model}}; v0.28.0: {install_id, fp} → то же для
    ЖИВОГО триала (известный install-ping fp, якорь не истёк; tier=trial).

    Только магазин. Self-hosted инстанс предъявляет license key → получает
    relay-токен (licensing.relay_token, TTL 7д): отзыв/истечение лицензии режут
    доступ к relay не позже TTL — конфиг перетягивается демоном. Публичный,
    rate-limit — как /api/license/validate.
    """
    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if _rate_limited(ip, "relayconfig", 20, 60):
        return JSONResponse({"ok": False, "error": "too_many_attempts"}, status_code=429)
    # body-кап ПОТОКОВО с ранним abort (SECURITY v0.25.0: request.body()
    # аллоцировал chunked-тело целиком до капа — memory-DoS магазина)
    body, body_err = await _read_capped(request, 16384)
    if body_err is not None:
        return body_err
    try:
        payload = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if not isinstance(payload, dict):  # [1,2] / "str" → 400, не 500
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    key = str(payload.get("key") or "")[:512].strip()
    if key:
        lic, err = licensing.verify_key(LICENSE_SECRET, key)
        revoked = lic is not None and _is_revoked(_load_codes(), key)
        if err or revoked:
            # единый ответ наружу (без оракула статуса ключа), деталь — в серверный лог
            log.info("relay/config отклонён от %s: %s", ip, err or "license revoked")
            _event("relay_config_rejected", "", err or "revoked")
            return JSONResponse({"ok": False, "error": "invalid key"}, status_code=403,
                                headers=_NOCACHE)
        exp = int(lic.get("exp") or 0)
        seed, tag, detail = key, lic.get("tier", ""), lic.get("email") or ""
    else:
        # v0.28.0: триальный грант {install_id, fp} — «семантика не выключается,
        # даже в триале» (директива владельца). Принимаем install_id, привязанный
        # к fp с ЖИВЫМ серверным якорем триала (install-ping, v0.16.3); грант
        # умирает вместе с триалом. NB: fp self-minted через install-ping —
        # смягчение фарма = per-IP rate-limit этого эндпоинта и install-ping
        # (10/час) + дневная квота на каждый trial-lk.
        iid = str(payload.get("install_id") or "")
        fp = str(payload.get("fp") or "")
        t = None
        pair_known = False
        if _INSTALL_ID_RE.match(iid) and _FP_RE.match(fp):
            try:
                with _installs_lock:
                    data = _load_installs() or {}
                    t = (data.get("trials") or {}).get(fp)
                    pair_known = f"{iid}:{fp}" in (data.get("relay_grants") or {})
            except Exception:  # битый installs.json → fail-closed (403), не 500
                t = None
            if not isinstance(t, dict) or iid not in (t.get("installs") or []):
                t = None  # fp известен, но install_id не привязан к нему
                pair_known = False
        try:
            started = float((t or {}).get("started_at") or 0)
        except (TypeError, ValueError, OverflowError):  # NaN/Infinity/мусор
            started = 0
        exp = int(started + licensing.trial_days() * 86400) if started else 0
        if not exp or time.time() > exp:
            log.info("relay/config trial-грант отклонён от %s (unknown install или якорь истёк)", ip)
            _event("relay_config_rejected", "", "trial grant")
            return JSONResponse({"ok": False, "error": "invalid key"}, status_code=403,
                                headers=_NOCACHE)
        # v0.40.0: кап — на НОВУЮ пару (install_id, fp) в сутки с IP (анти-фарм новых
        # личностей); повторный pull с той же пары (рестарт, watchdog) — бесплатно:
        # покупатель с рестартами в день установки больше не гаснет молча.
        if not pair_known:
            if _rate_limited(ip, "relaytrial", 2, 86400):
                log.info("relay/config trial-грант: дневной кап IP %s (новая пара)", ip)
                _event("relay_config_rejected", "", "trial daily cap")
                return JSONResponse({"ok": False, "error": "too_many_attempts"},
                                    status_code=429)
            with _installs_lock:  # запоминаем выдачу; записи старше TTL токена чистим
                gdata = _load_installs()
                if gdata is not None:  # битый файл — выдачу не блокируем, просто не запоминаем
                    grants = gdata.setdefault("relay_grants", {})
                    grants[f"{iid}:{fp}"] = time.time()
                    cut = time.time() - licensing.RELAY_TOKEN_TTL
                    for gk, gts in list(grants.items()):
                        if not isinstance(gts, (int, float)) or gts < cut:
                            del grants[gk]
                    try:
                        _save_installs(gdata)
                    except OSError:
                        pass  # диск полон — выдачу не блокируем, просто не запоминаем
        seed, tag, detail = f"trial:{fp}", "trial", "trial"
    ttl = int(time.time()) + licensing.RELAY_TOKEN_TTL
    tok_exp = min(exp, ttl) if exp else ttl
    _event("relay_config_issued", detail, tag)
    token = licensing.relay_token(licensing.relay_secret(LICENSE_SECRET), seed, tok_exp)
    out = {"ok": True, "embed": {
        "url": PUBLIC_BASE_URL + "/observer",
        "key": token,
        "model": _RELAY_EMBED_MODEL,
        "query_prefix": _RELAY_EMBED_QUERY_PREFIX,
        "passage_prefix": _RELAY_EMBED_PASSAGE_PREFIX},
        "rank": _rank_config_cached()}  # W3 (v0.27.0): словарь синонимов
    return JSONResponse(out, headers=_NOCACHE)


@mcp.custom_route("/api/tenants", methods=["GET"])
async def api_tenants_list(request: Request):
    """Учёт активаций для админки (v0.18.0): tenant ↔ email ↔ код ↔ licenseKey,
    флаг revoked. Bearer master ИЛИ admin-сессия (cookie, STORE_MODE)."""
    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    data = _load_codes()
    rows = []
    for t in data.get("tenants", []):
        lk = t.get("licenseKey") or ""
        rows.append({"id": t.get("id"), "email": t.get("email"), "tier": t.get("tier"),
                     "code": t.get("code"), "createdAt": t.get("createdAt"),
                     "paidAt": t.get("paidAt"), "licenseKey": lk,
                     "revoked": bool(lk) and bool(_is_revoked(data, lk))})
    rows.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
    return JSONResponse({"tenants": rows[:500]}, headers=_NOCACHE)


# ------------------------------------------------------------------ admin auth (v0.18.0) ----
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
                       "/api/admin/synonyms", "/api/admin/synonyms/veto"}  # W3 (v0.27.0)


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


# login-лимит считает ТОЛЬКО неудачи: иначе владелец с нескольких вкладок
# сам себя локает, а атакующий перманентно блокирует вход. Успех — бакет чистится.
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
    return JSONResponse({"configured": cfg is None or bool(cfg.get("pass_hash")),
                         "authed": _admin_auth(request)}, headers=_NOCACHE)


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
    """Отзыв/восстановление license key (v0.18.0, анти-чарджбэк). Bearer-only.

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
                         "first_seen": rec.get("first_seen"), "updated": rec.get("updated"),
                         "limit": _MACHINES_MAX_FP}, headers=_NOCACHE)


@mcp.custom_route("/api/admin/machines/unbind", methods=["POST"])
async def api_admin_machines_unbind(request: Request):
    """Отвязка машины от ключа — ТОЛЬКО владельцем (переноса самообслуживанием нет,
    решение владельца №4). {key, fp} → удалить fp из привязок ключа. Мутация
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
            changed = fp in fps
            if changed:
                rec["fps"] = [x for x in fps if x != fp]
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
    # опциональная привязка к кабинету: key = токен кабинета (mg_…) → платёж за тенантом
    tenant = None
    key = str(body.get("key") or "")[:128]
    if key:
        tenant = verify_tenant(key)
        if not tenant:
            return JSONResponse({"error": "unknown cabinet key"}, status_code=403)
        email = tenant.get("email") or email  # email кабинета авторитетнее
    if not email:
        return JSONResponse({"error": "valid email required"}, status_code=400)
    # v0.37.0: промокод рефералки (кука mg_ref → buybox шлёт body.ref) — −20% на
    # платёж тарифа (донатам фикс-цена). Невалидный код → полная цена, без ошибки.
    # shortcut: «первая покупка» по email не проверяется (SPEC-referral §2, антифрод по email).
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
        if tenant:
            rec["tenant"] = tenant["id"]
        data["payments"].append(rec)
        _save_codes(data)
    _event("payment_created", ref, f"{plan_id} · {email}")
    return JSONResponse({"payment_url": pay["payment_url"]}, headers=_NOCACHE)


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
        # v0.33.2: донат — без кода и ключа, просто спасибо
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
<div class="lbl">Код кабинета</div><div class="code">{code}</div>
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
    donation_delivery = None  # v0.33.2: (email, label) — thank-you письмо донатеру
    ref_msgs = []  # v0.37.0: TG владельцу о реф-событиях — отправка строго после локов
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
                # v0.33.2: донат — чистая поддержка: без кода/ключа/лицензии.
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
                # v0.18.0: продление — дни плана поверх max(старый exp, сейчас), остаток не сгорает
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
                # license key: tier всегда pro (v0.13.0), срок = длительность плана со дня оплаты
                email = (payment.get("email") or _valid_email(body.get("email"))) or None
                lic_key = licensing.generate_key(LICENSE_SECRET, email or "buyer@unknown", "pro", days)
                payment["licenseKey"] = lic_key
                if ext_ok:
                    # продление = ротация (как recovery v0.22.3): старый ключ гасим с grace 72ч,
                    # иначе один ключ мультиплицирует дни повторными покупками
                    _add_revoked(data, ext, "superseded: продление")
                if email:
                    payment["email"] = email
                # покупка из кабинета (платёж привязан к тенанту) → код сразу активирован,
                # license key закреплён за кабинетом (виден в /api/cabinet)
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
                        _event("cabinet_paid", tenant_id, f"{plan_id} · {code}")
                delivery = (code, lic_key, "pro", email, payment.get("ref") or pid)
                _event("payment", pid, f"{plan_id} · {amount}{currency} · код {code}")
                result["code"] = code
                # --- v0.37.0: рефералка (SPEC-referral §3.3–3.4) — начисление
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
                                        rrec["history"] = rrec["history"][-200:]  # L4
                                        ref_msgs.append(f"⚠️ Self-ref блок: код {ref_code} · "
                                                        f"{amount:.0f} {currency} · начисление отклонено")
                                        _event("ref_self_blocked", ref_code, f"{amount:.0f}{currency}")
                                    else:
                                        rrec["buys"] = _safe_int(rrec.get("buys")) + 1
                                        rrec["stack_n"] = min(_safe_int(rrec.get("stack_n")) + 1,
                                                              REF_STACK_CAP)
                                        rrec.setdefault("history", []).append(hist)
                                        rrec["history"] = rrec["history"][-200:]  # L4
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
    if donation_delivery:  # v0.33.2: thank-you донатеру — сеть строго вне лока, best-effort
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
    if ref_msgs:  # v0.37.0: TG владельцу о реф-продаже/self-ref — после локов, best-effort
        import asyncio
        tg_token = os.environ.get("TG_BOT_TOKEN", "")
        if TG_NOTIFY_CHAT_ID and tg_token:
            for msg in ref_msgs:
                try:
                    await asyncio.to_thread(billing.send_telegram, TG_NOTIFY_CHAT_ID, msg, tg_token)
                except Exception as e:
                    log.error("ref telegram → %s", e)
    return JSONResponse(result, headers=_NOCACHE)


# --- digiseller per-order (v0.7) ---

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

    Контракт и настройка кабинета — внутренние заметки владельца (вне репо). Идемпотентно по order id
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
    return _dg_response(code=res["code"], fmt=fmt, product_id=ok_id, inv=ok_inv)


# --- digiseller уведомления (v0.9.1): sale/message → webhooks.jsonl + Telegram ---

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


# --- telegram deep-link доставка (v0.11): t.me/Maxgraph_bot?start=<ref> → код в личку ---

def _tg_on_start(chat_id: str, payload: str) -> str:
    """Deep-link /start <ref> → код+license key платежа в личку.

    ref (r-<16 hex>, 64 бита) раньше показывался на success-странице (ссылка
    убрана в v0.27.2 — бот не работает с покупателями); обработчик оставлен
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


# --- embed (В2.2): фоновый пополнитель векторов для гибридного поиска ---

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
    """v0.19.0: embed включился в рантайме (relay zero-config после активации
    лицензии) — поднять цикл, если при boot он не стартовал (embed был выключен).
    Идемпотентно; READONLY-зеркало не пишет — не стартуем."""
    global EMBED_ON
    if READONLY or not embed.enabled():
        return
    with _embed_start_lock:
        if any(t.name == "embed" and t.is_alive() for t in threading.enumerate()):
            return
        threading.Thread(target=embed_loop, args=(embed.config_from_env(),),
                         daemon=True, name="embed").start()
        EMBED_ON = True
        log.info("embed: enabled в рантайме (relay zero-config)")


# --- actr (В2.3): периодический flush счётчиков обращений → access.json ---

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
    (лендинг, оболочки /app и кабинета, redeem/trial, биллинг, вебхуки, /healthz).

    Master-токен — ТОЛЬКО точное совпадение `Authorization: Bearer <MEMORY_TOKEN>`:
    аутентификации по query нет — `?token=` удалён в v0.11: токен не должен
    жить в access-логах uvicorn/nginx.

    Токен кабинета (mg_…) проверяет сам хендлер /api/cabinet — ТОЛЬКО Bearer
    (legacy-вход `?key=` удалён в v0.12.2: токены не живут в access-логах);
    это НЕ master-токен и не зона middleware. /admin — HTML-оболочка только на инстансе магазина (STORE_MODE=1,
    иначе 404); её данные идут через непубличные /api/* под Bearer.
    """

    PUBLIC_PATHS = {"/healthz", "/", "/cabinet", "/admin", "/app", "/landing", "/og.png",
                    "/privacy", "/offer",  # правовые страницы лендинга
                    "/api/codes/redeem", "/api/codes/trial", "/api/cabinet", "/api/plans",
                    "/api/billing/pay", "/api/billing/status", "/billing/result", "/billing/webhook/platega",
                    "/billing/create", "/billing/success", "/api/license/validate",
                    "/api/telemetry/install",  # v0.14: анонимный install-ping
                    "/api/telemetry/learn",  # v0.26.0: анонимные learn-events (W2)
                    "/api/version",  # v0.18.2: апдейт-чип дашборда (публичная версия)
                    "/api/admin/me",  # v0.18.0: страница входа админки (configured/authed — не секрет)
                    "/api/admin/login", "/api/admin/setup",  # v0.18.0: логин/первичная настройка (rate-limited)
                    "/api/relay/config",  # v0.19.0: zero-config hosted embed (license key → relay-токен)
                    "/api/referral/code",  # v0.37.0: реф-код инстанса (install_id+fp → code+статы)
                    "/api/releases/latest.tar.gz", "/api/releases/latest.sha256",  # v0.20.0: автообновления
                    "/api/releases/latest.sig",  # v0.20.0: ed25519-подпись релиза
                    "/billing/digiseller/gen",  # --- digiseller per-order (v0.7) ---
                    "/webhook/digiseller/sale", "/webhook/digiseller/message",  # уведомления (v0.9.1)
                    "/api/hooks/github"}  # v0.33.0: GitHub-вебхук витрины → TG владельцу

    # Префиксные публичные пути: статика лендинга (self-hosted шрифты).
    PUBLIC_PREFIXES = ("/static/",)

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
            # v0.38.1: канонический хост self-hosted UI — localhost. Токен дашборда живёт
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
                    # v0.18.0: админка магазина — сессионная кука; хендлер сам проверит
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
    _migrate_to_shards()  # v0.34.0: одноразовая разрезка memory.json → projects/ (с бэкапом)
    st = _license_status()  # при старте: создаёт license.json на первом запуске, ловит fail-closed
    log.info("license: %s (days_left=%s)%s", st.get("state"), st.get("days_left"),
             "" if licensing.allows_write(st) else " — МУТАЦИИ ЗАБЛОКИРОВАНЫ, activate: /#activate")
    # полный токен — ровно один раз при генерации (онбординг «токен в логах»);
    # при чтении из файла молчим — токен не тонет в журналах при каждом рестарте
    if _TOKEN_SOURCE == "generated":
        log.info("MEMORY_TOKEN сгенерирован и сохранён в data/local_token.txt: %s", TOKEN)
    elif _TOKEN_SOURCE == "ephemeral":
        log.warning("MEMORY_TOKEN: data/ read-only — эфемерный токен процесса: %s", TOKEN)
    if not STORE_MODE:
        threading.Thread(target=_bootstrap_hosted, daemon=True, name="bootstrap-hosted").start()  # v0.28.0: ping → relay-pull при boot
        threading.Thread(target=_license_watchdog, daemon=True, name="license-watchdog").start()  # v0.18.0
        learn.start_learn_loop(post=_post_json, base_url=PUBLIC_BASE_URL)  # W2 (v0.26.0)
    rankcfg.load(RANK_CONFIG_FILE)  # W3 (v0.27.0): last-good словарь синонимов
    env_key = os.environ.get("LICENSE_KEY", "").strip()  # лицензия из env (self-hosted pro)
    if env_key and st.get("state") != "licensed":
        res = _activate_key(env_key)
        log.info("LICENSE_KEY: %s", f"ok ({res.get('tier')} · {res.get('email')})" if res.get("ok")
                                   else f"отклонён: {res.get('error')}")
    migrate_token_hashes()  # одноразово: легаси plaintext-токены в codes.json → sha256 (№6)
    app = TokenAuth(mcp.streamable_http_app())
    if os.environ.get("TG_BOT_TOKEN"):
        try:
            import tgbot
            tgbot.start_poller(os.environ["TG_BOT_TOKEN"], _tg_on_start)  # daemon-поток, getUpdates
            log.info("tgbot: deep-link доставка активна (/start <ref>)")
        except ImportError:
            log.warning("tgbot: TG_BOT_TOKEN задан, но компонент отсутствует в этой сборке")
    global EMBED_ON
    emb_cfg = embed.config_from_env()  # В2.2: семантический слой (опционально, офлайн-safe)
    EMBED_ON = embed.should_start(emb_cfg, READONLY)
    if EMBED_ON:  # READONLY-зеркало не пишет → не запускаем
        threading.Thread(target=embed_loop, args=(emb_cfg,), daemon=True,
                         name="embed").start()
        log.info("embed: enabled (model=%s, hybrid_w=%s)", emb_cfg["model"],
                 emb_cfg["hybrid_weight"])
    else:
        log.info("embed: disabled (%s)",
                 "READONLY mirror" if embed.enabled(emb_cfg) else "no EMBED_URL/KEY")
    # В2.3: ACT-R reinforcement. Буст default ON (ACTR_MAX_BOOST=0 → off).
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
    log.info("maxgraph %s listening on 0.0.0.0:%s (store: %s, store_mode=%s, trust_proxy=%s, readonly=%s)",
             VERSION, PORT, MEMORY_FILE, STORE_MODE, TRUST_PROXY, READONLY)
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info", proxy_headers=True)


if __name__ == "__main__":
    main()
