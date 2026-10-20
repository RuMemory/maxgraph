#!/usr/bin/env python3














































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
import unicodedata
import urllib.parse
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.responses import FileResponse
from starlette.responses import Response

import licensing
import hostfp
import plans
import searchrank
import embed
import actr
import learn
import rankcfg



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

_FROZEN = getattr(sys, "frozen", False)

_RESOURCE_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _default_data_dir(env=None, frozen=None) -> str:

    env = os.environ if env is None else env
    frozen = _FROZEN if frozen is None else frozen
    if env.get("MEMORY_DIR"):
        return env["MEMORY_DIR"]
    return str(Path(sys.executable).resolve().parent / "data") if frozen else "/data"


def _bind_host(env=None, frozen=None) -> str:

    env = os.environ if env is None else env
    frozen = _FROZEN if frozen is None else frozen
    return env.get("MG_HOST") or ("127.0.0.1" if frozen else "0.0.0.0")


def _find_version() -> str:

    for p in (_RESOURCE_BASE / "VERSION", _RESOURCE_BASE.parent / "VERSION"):
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    return "0.0.0-dev"


_TOKEN_SOURCE = ""


def _resolve_token(env=None, data_dir=None) -> str:




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
            pass
        _TOKEN_SOURCE = "generated"
        return tok
    except OSError:
        _TOKEN_SOURCE = "ephemeral"
        return "mg_local_" + secrets.token_hex(24)


def _require_store_token():


    if STORE_MODE and not _ENV_TOKEN:
        raise SystemExit("STORE_MODE=1 требует явный MEMORY_TOKEN в env (автогенерация запрещена)")


VERSION = _find_version()
DATA_DIR = Path(_default_data_dir())
HOST = _bind_host()
MEMORY_FILE = DATA_DIR / "memory.json"
PROJECTS_DIR = DATA_DIR / "projects"
EVENTS_FILE = DATA_DIR / "events.jsonl"
PROJECT_ALIASES_FILE = DATA_DIR / "project-aliases.json"
INSTALLS_FILE = DATA_DIR / "installs.json"
LEARN_EVENTS_FILE = DATA_DIR / "learn_events.jsonl"
HEARTBEATS_FILE = DATA_DIR / "heartbeats.jsonl"
SUPPORT_REPORTS_FILE = DATA_DIR / "support_reports.jsonl"
UPDATE_LOG_FILE = DATA_DIR / "update.log"
SERVER_LOG_FILE = DATA_DIR / "maxgraph.log"
RANK_CONFIG_FILE = DATA_DIR / "rank_config.json"
_ENV_TOKEN = bool((os.environ.get("MEMORY_TOKEN") or "").strip())
TOKEN = _resolve_token()
WEBHOOK_SECRET = os.environ.get("PLATEGA_WEBHOOK_SECRET", "")
PLATEGA_MERCHANT_ID = os.environ.get("PLATEGA_MERCHANT_ID", "")
PLATEGA_API_KEY = os.environ.get("PLATEGA_API_KEY", "")
PLATEGA_API = os.environ.get("PLATEGA_API_BASE") or (billing.DEFAULT_API_BASE if billing else "")
PLATEGA_CURRENCY = os.environ.get("PLATEGA_CURRENCY") or "RUB"


LAVA_API_KEY = os.environ.get("LAVA_API_KEY", "")
LAVA_WEBHOOK_KEY = os.environ.get("LAVA_WEBHOOK_KEY", "")
LAVA_OFFER_ID = os.environ.get("LAVA_OFFER_ID") or (lava.DEFAULT_OFFER_ID if lava else "")
LAVA_API = os.environ.get("LAVA_API_BASE") or (lava.DEFAULT_API_BASE if lava else "")
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "https://rezon-ai.ru").rstrip("/")



RELEASES_DIR = os.environ.get("RELEASES_DIR") or str(DATA_DIR / "releases")




_PLAN_DAYS = {"pro1": 31, "pro3": 93, "pro6": 186, "pro12": 372}
PLANS = {pid: {**plans.PRICES[pid], "days": _PLAN_DAYS[pid], "label": plans.LABELS[pid]}
         for pid in _PLAN_DAYS}


_DONATE = ("donate99", "donate249", "donate499")
PLANS.update({pid: {**plans.PRICES[pid], "days": 0, "label": plans.LABELS[pid],
                    "donation": True} for pid in _DONATE})
PLAN_ALIASES = {"pro": "pro1", "team": "pro1"}


def _plan_key(name: str) -> str:

    pid = (name or "").strip()
    if pid in PLAN_ALIASES:
        return PLAN_ALIASES[pid]
    return pid if pid in PLANS else ""

DIGISELLER_GEN_SECRET = os.environ.get("DIGISELLER_GEN_SECRET", "")
DIGISELLER_SIGN_SCHEME = os.environ.get("DIGISELLER_SIGN_SCHEME", "url-key")
DIGISELLER_RESPONSE_FORMAT = os.environ.get("DIGISELLER_RESPONSE_FORMAT", "text")
DIGISELLER_PRODUCT_ID = os.environ.get("DIGISELLER_PRODUCT_ID", "XXXXXXXX")

DIGISELLER_HOOK_SECRET = os.environ.get("DIGISELLER_HOOK_SECRET", "")
TG_NOTIFY_CHAT_ID = os.environ.get("TG_NOTIFY_CHAT_ID", "")

STORE_MODE = (os.environ.get("STORE_MODE") or "").strip() == "1"


READONLY = (os.environ.get("READONLY") or "").strip() == "1"
WEBHOOKS_LOG = DATA_DIR / "webhooks.jsonl"
EMBEDDINGS_FILE = DATA_DIR / "embeddings.json"
EMBED_STATE_FILE = DATA_DIR / "embed.state.json"
ACCESS_FILE = DATA_DIR / "access.json"
LICENSE_SECRET = licensing.get_secret(DATA_DIR)
LICENSE_SUB_DAYS = 31
EXTEND_DISCOUNT = 0.9



REF_HMAC_SECRET = (os.environ.get("REF_HMAC_SECRET") or "").strip() or "dev-only-ref-secret"
REF_DISCOUNT = 0.8
REF_STACK_CAP = 4
PORT = int(os.environ.get("PORT", "8787"))
STARTED = time.time()
EMBED_ON = False
EVENTS_CAP = 10000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
if _FROZEN:
    try:
        from logging.handlers import RotatingFileHandler
        _fh = RotatingFileHandler(DATA_DIR / "maxgraph.log", maxBytes=1_000_000,
                                  backupCount=2, encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(_fh)
    except OSError:
        pass
log = logging.getLogger("maxgraph")



if STORE_MODE and REF_HMAC_SECRET == "dev-only-ref-secret":

    raise SystemExit("REF_HMAC_SECRET не задан в .env — задай секрет (магазин без него не стартует)")

_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")




def _load():






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
                if rec.get("project"):
                    ent["project"] = rec["project"]
                if rec.get("status"):
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


    seen = set()
    relations = [r for r in relations
                 if (r["from"], r["to"], r["relationType"]) not in seen
                 and not seen.add((r["from"], r["to"], r["relationType"]))]
    return entities, relations


_shard_written = {}




_RESERVED_SLUGS = frozenset({"con", "prn", "aux", "nul"}
                            | {f"com{i}" for i in range(1, 10)}
                            | {f"lpt{i}" for i in range(1, 10)})


def _shard_of_entity(e: dict) -> str:


    p = e.get("project") or ""
    return p if len(p) <= 64 and _PROJECT_RE.fullmatch(p) and p not in _RESERVED_SLUGS else ""


def _partition_shards(entities: dict, relations: list) -> dict:



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




    DATA_DIR.mkdir(parents=True, exist_ok=True)
    shards = _partition_shards(entities, relations)
    if PROJECTS_DIR.is_dir():
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
        for path, h, text in pending:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            tmps.append((tmp, path, h))
        for tmp, path, h in tmps:
            tmp.replace(path)
            _shard_written[path] = h
    finally:
        for tmp, _, _ in tmps:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


def _migrate_to_shards():







    sentinel = PROJECTS_DIR / ".sharded"
    if READONLY or not MEMORY_FILE.exists() or sentinel.exists():
        return
    backup = DATA_DIR / "memory.json.pre-shards-backup"
    if not backup.exists():
        backup.write_bytes(MEMORY_FILE.read_bytes())
    ents, rels = _load()
    _save(ents, rels)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    sentinel.write_text("ok\n", encoding="ascii")
    log.info("шардинг: memory.json разрезан (%d сущностей, %d связей), слепок → %s",
             len(ents), len(rels), backup.name)


def _graph_file_bytes() -> int:

    total = MEMORY_FILE.stat().st_size if MEMORY_FILE.exists() else 0
    if PROJECTS_DIR.is_dir():
        total += sum(f.stat().st_size for f in PROJECTS_DIR.glob("*.json"))
    return total


def _test_purge_graph_files():


    if MEMORY_FILE.exists():
        MEMORY_FILE.unlink()
    if PROJECTS_DIR.is_dir():
        for f in PROJECTS_DIR.glob("*.json"):
            f.unlink()
    _shard_written.clear()


def _event(action: str, entity: str = "", detail: str = "", project: str = "",
           source: str = "", host: str = ""):




    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ev = {"ts": now_iso(), "action": action, "entity": entity, "detail": detail}
        if project:
            ev["project"] = project
        if source:
            ev["source"] = source
        if host:
            ev["host"] = host
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

        if EVENTS_FILE.stat().st_size > 4_000_000:
            lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()[-EVENTS_CAP:]
            EVENTS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as ex:
        log.warning("event log failed: %s", ex)










_actr = actr.Store()
_actr.load(ACCESS_FILE)


def _touch(names):





    _actr.touch(names)


def _actr_boost_fn():




    cfg = actr.config_from_env()
    if cfg["max_boost"] <= 0.0:
        return None
    return actr.boost_fn(_actr, cfg)










_PROJECT_RE = re.compile(r"[^\W_]+(-[^\W_]+)*")






_RU2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _canon_slug(s) -> str:



    s = unicodedata.normalize("NFC", str(s or "").strip().lower())
    return "".join(_RU2LAT.get(ch, ch) for ch in s)


def _check_project(value) -> tuple[str, str]:








    slug = _canon_slug(value)



    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:


        if str(value or "").strip():
            return "", (f"invalid project slug {value!r}: пусто после "
                        f"нормализации (транслит/NFC)")
        return "", ""
    if len(slug) > 64 or len(slug.encode("utf-8")) > 200 \
            or not _PROJECT_RE.fullmatch(slug) or slug in _RESERVED_SLUGS:
        return "", (f"invalid project slug {value!r}: kebab из букв/цифр любого "
                    f"алфавита + дефисы, ≤64 chars")
    return slug, ""


def _truthy(value) -> bool:

    return str(value or "").strip().lower() in ("1", "true", "yes")


def _query_project(request) -> tuple[str, str]:

    return _check_project(request.query_params.get("project"))


def _project_entity_name(slug: str) -> str:
    return f"project:{slug}"


def _in_project(entity: dict, slug: str) -> bool:





    fam = {_canon_slug(s) for s in _project_family(slug)}
    p = _canon_slug(entity.get("project") or "")
    if p in fam:
        return True
    name = entity["name"]
    return name.startswith("project:") and _canon_slug(name[8:]) in fam - {""}


def _filter_graph(ents: dict, rels: list, slug: str, all_projects: bool):


    if not slug or all_projects:
        return list(ents.values()), rels
    es = [e for e in ents.values() if _in_project(e, slug)]
    names = {e["name"] for e in es}
    return es, [r for r in rels if r["from"] in names and r["to"] in names]












_REPO_TAG_RE = re.compile(r"^repo-[^\W_]+(-[^\W_]+)*-[0-9a-f]{12}$")
_ALIASES_CAP = 10_000
_aliases_cache = None


def _aliases_load() -> dict:


    global _aliases_cache
    if _aliases_cache is not None:
        return _aliases_cache
    data = {}
    try:
        if PROJECT_ALIASES_FILE.exists():
            raw = json.loads(PROJECT_ALIASES_FILE.read_text(encoding="utf-8"))
            for k, v in (raw.get("aliases") or {}).items():




                if (isinstance(k, str) and isinstance(v, str)
                        and len(k) <= 64 and len(v) <= 64
                        and _PROJECT_RE.fullmatch(k) and _PROJECT_RE.fullmatch(v)
                        and k not in _RESERVED_SLUGS and v not in _RESERVED_SLUGS):
                    data[k] = v
                    if len(data) >= _ALIASES_CAP:
                        break
    except Exception as ex:
        log.warning("project-aliases: сбой чтения: %s", ex)
    _aliases_cache = data
    return _aliases_cache


def _aliases_save(aliases: dict):


    global _aliases_cache
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PROJECT_ALIASES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"aliases": aliases}, ensure_ascii=False,
                                  sort_keys=True), encoding="utf-8")
        tmp.replace(PROJECT_ALIASES_FILE)
        try:
            os.chmod(PROJECT_ALIASES_FILE, 0o600)
        except OSError:
            pass
        _aliases_cache = dict(aliases)
    except Exception as ex:
        log.warning("project-aliases: сбой записи: %s", ex)


def _resolve_project(slug: str) -> str:



    return _aliases_load().get(slug, slug) if slug else slug


def _project_family(slug: str) -> frozenset:




    if not slug:
        return frozenset({""})
    canon = _resolve_project(slug)
    fam = {canon}
    for k, v in _aliases_load().items():
        if v == canon:
            fam.add(k)
    return frozenset(fam)


def _slug_in_family(p: str, fam) -> bool:



    return _canon_slug(p) in {_canon_slug(s) for s in fam}


def _canon_collisions(ents: dict) -> dict:




    by_canon = {}
    for e in ents.values():
        raw = e.get("project") or ""
        if raw:
            by_canon.setdefault(_canon_slug(raw), set()).add(raw)
    return {c: sorted(raws) for c, raws in by_canon.items() if len(raws) > 1}








CANONICAL_TYPES = ("project", "session", "bug", "decision", "request", "event", "task")

_TYPE_SYNONYMS = {

    "project": "project", "проект": "project",
    "session": "session", "сессия": "session",
    "bug": "bug", "bugfix": "bug", "fix": "bug", "hotfix": "bug", "defect": "bug",
    "баг": "bug", "багфикс": "bug", "фикс": "bug", "ошибка": "bug",
    "decision": "decision", "решение": "decision",
    "request": "request", "feature-request": "request",
    "просьба": "request", "запрос": "request", "хотелка": "request",
    "event": "event", "событие": "event",

    "task": "task", "todo": "task", "задача": "task", "туду": "task", "дело": "task",
}



BUG_STATUSES = ("open", "fixed")
TASK_STATUSES = ("open", "done")

_STATUS_BY_TYPE = {"bug": BUG_STATUSES, "task": TASK_STATUSES}


def _canonical_type(value) -> str:


    t = str(value or "").strip()
    if not t:
        return "general"
    return _TYPE_SYNONYMS.get(t.lower(), t)


def _check_status(value, etype: str) -> tuple[str, str]:







    st = str(value or "").strip().lower()
    if not st:
        return "", ""
    valid = _STATUS_BY_TYPE.get(etype)
    if valid and st not in valid:
        return "", f"invalid {etype} status {value!r}: {'|'.join(valid)}"
    return st, ""




def _norm_obs(s) -> str:




    r = " ".join(str(s).casefold().split()).strip(" .,;:!?…")
    return r or str(s).strip()








_INVALIDATED_RE = re.compile(r"^\[~[0-9]{4}-[0-9]{2}-[0-9]{2}\]\s*")


def _is_invalidated(obs) -> bool:
    s = str(obs)
    return s.startswith("[~") and bool(_INVALIDATED_RE.match(s))


def _strip_marker_on_write(text):



    s = str(text)
    if _is_invalidated(s):
        return _INVALIDATED_RE.sub("", s, count=1), True
    return s, False


def _valid_obs(e: dict) -> list:

    return [o for o in e.get("observations", []) if not _is_invalidated(o)]


def _mark_observation(obs: str, today: str) -> str:

    return obs if _is_invalidated(obs) else f"[~{today}] {obs}"







def _obs_id(text) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:12]


def _obs_with_ids(obs_list) -> list:

    return [{"id": _obs_id(o), "text": o} for o in obs_list]



def _create_entities_graph(ents, rels, items, project: str = "", source: str = "",
                           mk_project: bool = True):










    created = []
    warnings = []
    merged = obs_added = obs_dedup = adopted = 0
    ts = now_iso()
    rel_keys = {(r["from"], r["to"], r["relationType"]) for r in rels}
    rels_added = False

    def link_part_of(name: str):
        nonlocal rels_added
        key = (name, _project_entity_name(project), "part of")
        if key[1] not in ents:



            return
        if key in rel_keys:
            return
        rels.append({"from": name, "to": key[1], "relationType": "part of", "createdAt": ts})
        rel_keys.add(key)
        rels_added = True
        _event("create_relation", f"{name}→{key[1]}", "part of", project=project,
               source=source)

    project_created = False
    if project and mk_project:
        pname = _project_entity_name(project)
        if pname not in ents:
            ents[pname] = {"name": pname, "title": project, "entityType": "project",
                           "tags": [], "observations": [], "createdAt": ts, "updatedAt": ts}
            project_created = True
            _event("create_entity", pname, project, project=project, source=source)
    for e in items:
        if not str(e.get("name") or "").strip():


            warnings.append("пустое name — сущность пропущена")
            continue
        if e["name"] in ents:
            ex = ents[e["name"]]
            ex_project = ex.get("project") or ""
            if project and ex_project \
                    and _canon_slug(ex_project) != _canon_slug(project):
                warnings.append(
                    f"'{e['name']}' уже существует в проекте '{ex_project}' — merge "
                    f"в проект '{project}' отклонён (имена сущностей глобально уникальны)")
                continue



            if project and e["name"].startswith("project:") \
                    and not _slug_in_family(e["name"][8:], _project_family(project)):
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

            warnings.append(
                f"'{e['name']}' уже существует — факты долиты merge'ом "
                f"(новых: {len(added)}, дублей: "
                f"{len(e.get('observations', [])) - len(added)}), "
                f"новая сущность не создана")
            if project and not ex_project and not e["name"].startswith("project:"):
                ex["project"] = project
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






        if etype == "project":
            n = e["name"]
            own_marker = bool(project) and n.startswith("project:") \
                and _slug_in_family(n[8:], _project_family(project))
            if not own_marker and n != project:
                warnings.append(
                    f"'{n}': entityType=project зарезервирован для "
                    f"проектов-рабочих-папок — используйте свободный тип "
                    f"(product/service/topic/…); сущность не создана")
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

            d["status"] = status or "open"
        elif status:
            warnings.append(f"'{e['name']}': status применим только к bug/task "
                            f"(тип '{etype}') — проигнорирован")


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























    project = _resolve_project(project)
    with _lock:
        ents, rels = _load()
        out, changed = _create_entities_graph(ents, rels, items, project, source)
        if changed:
            _save(ents, rels)
        scope = {n for n, e in ents.items()
                 if not project
                 or _canon_slug(e.get("project") or "") == _canon_slug(project)}
        _reprime_tick(project, out)
    nd = _neardup_warnings(out.get("created") or [], scope)
    if nd:
        out.setdefault("warnings", []).extend(nd)




    created = out.get("created") or []
    touched = [e["name"] for e in created]


    new_texts = [" ".join(str(o) for o in e.get("observations", []))
                 for e in created[:_HINT_TEXTS_MAX]]
    hints = _quality_hints(ents, rels, touched, new_texts, project)
    if hints:
        out["hints"] = hints
    return out


_NEARDUP_COS = 0.85
_NEARDUP_MAX = 3
_NEARDUP_CHECK_MAX = 20





def _neardup_warnings(created: list, scope: set) -> list:






    if not created:
        return []
    try:
        cfg = embed.config_from_env()
        if not embed.enabled(cfg):
            return []
        store = embed.load_store(str(EMBEDDINGS_FILE), model=cfg["model"])
        if not store:
            return []
        cands = [n for n in scope if n in store]
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
        log.warning("neardup check skipped: %s", ex)
        return []










_HINT_SIMILAR_COS = 0.92
_HINT_SIMILAR_CONTAIN = 0.8
_HINTS_MAX = 3
_HINT_TEXTS_MAX = 5
_HINT_OBS_SPLIT = 15


def _similar_hints(new_texts, ents, project, self_names) -> list:





    texts = [str(t or "").strip() for t in (new_texts or [])]
    texts = [t for t in texts if t][:_HINT_TEXTS_MAX]
    if not texts:
        return []
    try:
        cfg = embed.config_from_env()
        store = {}
        if embed.enabled(cfg):
            store = embed.load_store(str(EMBEDDINGS_FILE), model=cfg["model"])
        scope = []
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
            if store:


                qv = embed.query_vector(text[:embed.ENTITY_TEXT_MAX], cfg=cfg,
                                        state_path=EMBED_STATE_FILE)
                if qv:
                    scorer = embed.cosine_scorer(store, qv)
                    for n, _e in scope:
                        s = scorer(n)
                        if s is not None and s >= _HINT_SIMILAR_COS:
                            hits.append((s, n, "embed"))


            if not hits and (not store or qv is None):
                qt = set(searchrank.tokenize(text))
                if len(qt) >= 3:
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
        log.warning("similar hints skipped: %s", ex)
        return []


def _hygiene_hints(ents, rels, touched_names) -> list:

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







    project = _resolve_project(project)
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



            if new_type == "project" and not name.startswith("project:") \
                    and name != project:
                warnings.append(
                    f"'{name}': entityType=project зарезервирован для "
                    f"проектов-рабочих-папок — тип не изменён")
                new_type = e["entityType"]
            if new_type != e["entityType"]:
                e["entityType"] = new_type
                if new_type in _STATUS_BY_TYPE:

                    if str(e.get("status") or "") not in _STATUS_BY_TYPE[new_type]:
                        e["status"] = "open"
                else:
                    e.pop("status", None)
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
    project = _resolve_project(project)
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










    project = _resolve_project(project)
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
                r["markerStripped"] = stripped
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

    hints = _quality_hints(ents, rels, touched, new_texts, project)
    if hints:
        out["hints"] = hints
    return out


def op_delete_entities(names, project: str = ""):


    project = _resolve_project(project)
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



    project = _resolve_project(project)
    if not project:
        return {"error": "project is required"}
    pname = _project_entity_name(project)
    pc = _canon_slug(project)
    with _lock:
        ents, rels = _load()
        coll = _canon_collisions(ents).get(pc)



        doomed = {n for n, e in ents.items()
                  if _canon_slug(e.get("project") or "") == pc or n == pname
                  or (n.startswith("project:") and _canon_slug(n[8:]) == pc)
                  or (_canon_slug(n) == pc and e.get("entityType") == "project")}





        aliases = {k: v for k, v in _aliases_load().items()
                   if _canon_slug(k) != pc and _canon_slug(v) != pc}

        aliases_changed = len(aliases) != len(_aliases_load())
        if not doomed and not aliases_changed:
            return {"deleted": 0, "relations_removed": 0}
        before = len(rels)
        rels = [r for r in rels
                if r.get("from") not in doomed and r.get("to") not in doomed]
        for n in doomed:
            del ents[n]
        if doomed:
            _save(ents, rels)
        if aliases_changed:
            _aliases_save(aliases)
        out = {"deleted": len(doomed), "relations_removed": before - len(rels)}
        if coll:


            log.warning("delete_project %s: удалён canon-класс с написаниями %s",
                        project, coll)
            out["canon_raw_slugs"] = coll
    _event("delete_project", project, f"{out['deleted']} entities",
           project=project)
    return out


def op_delete_observations(items, project: str = ""):

    project = _resolve_project(project)
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


def op_delete_observations_by_id(items, project: str = ""):





    project = _resolve_project(project)
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


    project = _resolve_project(project)
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
    canon = _resolve_project(project)
    with _lock:
        ents, rels = _load()
    es, rs = _filter_graph(ents, rels, canon, all_projects)
    out = {"entities": es, "relations": rs}
    if canon != project and project and not all_projects:
        out["meta"] = {"aliased_to": canon}
    return out


def _hybrid_search(query_str: str):








    cfg = embed.config_from_env()
    if not embed.enabled(cfg):
        return 0.0, None
    w = cfg["hybrid_weight"]
    if w <= 0.0:
        return 0.0, None
    store = embed.load_store(EMBEDDINGS_FILE)
    if not store:
        return 0.0, None
    qv = embed.query_vector(query_str, cfg=cfg, state_path=EMBED_STATE_FILE)
    if qv is None:
        return 0.0, None
    return w, embed.cosine_scorer(store, qv)


def op_search(query, project: str = "", all_projects: bool = False, type_filter: str = "",
              limit: int = searchrank.DEFAULT_LIMIT, with_ids: bool = False):






























    tf = _canonical_type(type_filter) if str(type_filter or "").strip() else ""
    project = _resolve_project(project)
    with _lock:
        ents, rels = _load()
    corpus = list(ents.values())
    if project and not all_projects:
        corpus = [e for e in corpus if _in_project(e, project)]
    if tf:
        corpus = [e for e in corpus if _canonical_type(e.get("entityType")) == tf]






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


    expanded_str = rankcfg.expand_query(query_str)
    qts = searchrank.tokenize(expanded_str)
    if query_str.strip() and qts:
        vec_w, cos_fn = _hybrid_search(query_str)
        pairs = searchrank.rank(corpus, expanded_str, limit=limit,
                                vector_weight=vec_w, cosine_fn=cos_fn,
                                actr_fn=_actr_boost_fn())
        ranked = True
    else:
        ranked = False
        if query_str.strip():

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


            st = str(e.get("status") or "").strip().lower()
            v["status"] = st if st in BUG_STATUSES else ("open" if _bug_is_open(e) else "fixed")
        elif v["type"] == "task":


            v["status"] = _task_status_out(e)
        if ranked:
            v["score"] = round(s, 4)
            text, highlights = searchrank.snippet(e, qts)
            v["snippet"] = text
            v["highlights"] = highlights
        if with_ids:
            v["observations"] = _obs_with_ids(v.get("observations", []))
        out_entities.append(v)
    if query_str.strip():



        _touch([e["name"] for e in out_entities[:actr.TOUCH_TOP]])


    if ranked:
        learn.record_search(query_str,
                            [(e["name"], e.get("title"))
                             for e in out_entities])
    else:
        learn.record_search("", [])
    return {"entities": out_entities, "relations": mrels}


def op_open(names, project: str = "", with_ids: bool = False):


    project = _resolve_project(project)
    want = set(names)
    with _lock:
        ents, rels = _load()
    if not project:

        found = [e for n, e in ents.items() if n in want]
        frels = [r for r in rels if r["from"] in want and r["to"] in want]
        _touch([e["name"] for e in found])
        learn.record_open([e["name"] for e in found])
    else:
        found = [e for n, e in ents.items() if n in want and _in_project(e, project)]
        found_names = {e["name"] for e in found}
        frels = [r for r in rels if r["from"] in found_names and r["to"] in found_names]
        _touch([e["name"] for e in found])
        learn.record_open([e["name"] for e in found])
    if with_ids:
        found = [dict(e, observations=_obs_with_ids(e.get("observations", [])))
                 for e in found]
    return {"entities": found, "relations": frels}





_TIMELINE_TECH = frozenset({"hook_PostToolUse", "hook_Stop"})


def op_timeline(limit=100, project: str = "", all_projects: bool = False,
                tools: bool = False):
    project = _resolve_project(project)
    with _lock:
        if not EVENTS_FILE.exists():
            return {"events": []}
        lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    events = []
    if project and not all_projects:


        fam = _project_family(project)
        for line in reversed(lines):
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if not tools and ev.get("action") in _TIMELINE_TECH:
                continue
            ep = ev.get("project")
            if not isinstance(ep, str):
                continue
            if _slug_in_family(ep, fam):
                events.append(ev)
                if len(events) >= limit:
                    break
        return {"events": events}
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not tools and ev.get("action") in _TIMELINE_TECH:
            continue
        events.append(ev)
        if len(events) >= limit:
            break
    return {"events": events}







_TASK_NAME_MAX = 64
_TASK_TITLE_MAX = 200
_TASK_OPEN_TASKS_MAX = 20



_TASK_SLUG_BAD = re.compile(r"[^a-z0-9]+")


def _task_slug(title) -> str:

    s = "".join(_RU2LAT.get(ch, ch) for ch in str(title or "").strip().lower())
    return _TASK_SLUG_BAD.sub("-", s).strip("-")


def _task_name_free(ents: dict, title: str, taken=()) -> str:




    slug = _task_slug(title)
    base = (f"task-{slug}" if slug else "task")[:_TASK_NAME_MAX]
    name, n = base, 2
    while name in ents or name in taken:
        suffix = f"-{n}"
        name = base[:_TASK_NAME_MAX - len(suffix)] + suffix
        n += 1
    return name


def _task_is_open(e: dict) -> bool:



    return str(e.get("status") or "").strip().lower() != "done"


def _task_status_out(e: dict) -> str:


    st = str(e.get("status") or "").strip().lower()
    return st if st in TASK_STATUSES else "open"


def op_task_add(title, note: str = "", project: str = "", source: str = "",
                links=()) -> dict:







    project = _resolve_project(project)
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

            result = {"error": f"task для «{title}» не создана"}
        else:
            result = {"created": out["created"][0]}
            if out.get("warnings"):
                result["warnings"] = out["warnings"]

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







    project = _resolve_project(project)
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






    project = _resolve_project(project)
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










_SESSION_CLOSE_TAG = "session-close"
_SESSION_LINKS_MAX = 20
_UNCLOSED_MIN_WRITES = 5
_UNCLOSED_TOUCHED_MAX = 5
_WRITE_ACTIONS = frozenset({
    "create_entity", "merge_entity", "create_relation", "update_entity",
    "add_observation", "delete_entity", "task_done", "session_close",
})









_REPRIME_MIN_WRITES = 5
_reprime_writes = {}


def _reprime_tick(project: str, out: dict) -> None:



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



    name, closed = "", False
    for e in ents.values():
        if e.get("entityType") != "session":
            continue
        if _canon_slug(e.get("project") or "") != _canon_slug(project):
            continue
        if str(e.get("createdAt", ""))[:10] != today:
            continue
        name = e["name"]
        if _SESSION_CLOSE_TAG in (e.get("tags") or []):
            closed = True
    return name, closed


def _touched_today(project: str, today: str, exclude: str = "", lines=None) -> list:




    if lines is None:
        if not EVENTS_FILE.exists():
            return []
        lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    names, seen = [], set()
    fam = _project_family(project)
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except Exception:
            continue
        ep = ev.get("project") or ""
        if not isinstance(ep, str):
            continue
        if not _slug_in_family(ep, fam):
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






    project = _resolve_project(project)
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
            while name in ents:
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

                if l in ents and _in_project(ents[l], project):
                    want.append(l)
                else:
                    skipped.append(l)
            auto = False
        else:
            want, skipped, auto = touched, [], True
        if len(want) > _SESSION_LINKS_MAX:
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







SESSION_START_BUDGET = 10000
_SS_CAP_PROJECT_FACTS = 5
_SS_CAP_DECISION_LINES = 50
_SS_CAP_BUG_LINES = 30
_SS_CAP_TASK_LINES = 10
_SS_CAP_SESSIONS = 10
_SS_CAP_EVENTS = 15


_SS_MARKER_RESERVE = 40




_BUG_STATUS_WORD = re.compile(
    r"\b(open|reopened|fixed|closed|resolved|done|открыт\w*|закрыт\w*|исправлен\w*|пофикш\w*)\b",
    re.IGNORECASE)
_BUG_OPEN_WORDS = {"open", "reopened"}


def _bug_word_is_open(word: str) -> bool:
    return word in _BUG_OPEN_WORDS or word.startswith("открыт")


def _bug_is_open(e: dict) -> bool:




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

    return (e.get("updatedAt") or e.get("createdAt") or "", e["name"])


def _ss_ts(e: dict) -> float:


    try:
        dt = datetime.fromisoformat(str(e.get("updatedAt") or e.get("createdAt") or ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _ss_sort_recent(entities, mult_fn=None):





    if mult_fn is None:
        return sorted(entities, key=_recency_key, reverse=True)

    def key(e):
        try:
            m = float(mult_fn(e["name"]))
        except Exception:
            m = 1.0
        if not 0.0 < m < float("inf"):
            m = 1.0
        return (_ss_ts(e) * m, e["name"])

    return sorted(entities, key=key, reverse=True)


def _ss_head(e: dict, field: str = "title") -> str:

    date = (e.get("updatedAt") or "")[:10]
    return f"- **{e[field]}**" + (f" ({date})" if date else "")


def _fit_budget(sections: list, budget: int):








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
            cutting = True
            dropped += len(body) + 1
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

        out.append(marker)
    elif not out and len(marker) <= budget:
        out = [marker]
    text = "\n".join(out)
    if len(text) > budget:
        text, shown = "", [0] * len(sections)
    return text, shown, True


def op_session_start(project: str, budget_chars: int = SESSION_START_BUDGET,
                     birth: bool = True):





































    canon = _resolve_project(project)
    aliased_to = canon if canon != project else ""
    project = canon
    if birth and project:
        pname0 = _project_entity_name(project)
        with _lock:
            ents0, rels0 = _load()
            if pname0 not in ents0:




                ents0[pname0] = {"name": pname0, "title": project,
                                 "entityType": "project", "tags": [],
                                 "observations": [], "createdAt": now_iso(),
                                 "updatedAt": now_iso()}
                _event("create_entity", pname0, project, source="agent")
                _save(ents0, rels0)
    try:
        budget = max(0, int(budget_chars))
    except (TypeError, ValueError):
        budget = SESSION_START_BUDGET
    mult_fn = _actr_boost_fn()
    with _lock:
        ents, _rels = _load()
        ev_lines = (EVENTS_FILE.read_text(encoding="utf-8").splitlines()
                    if EVENTS_FILE.exists() else [])
        _reprime_writes[project] = 0

    fam = _project_family(project)
    fam_canon = {_canon_slug(s) for s in fam} - {""}
    proj_ent = None
    for e in ents.values():
        if e["name"].startswith("project:") \
                and _canon_slug(e["name"][8:]) in fam_canon:
            proj_ent = e
            break
    proj_ents = [e for e in ents.values()
                 if _slug_in_family(e.get("project") or "", fam)]

    proj_facts = _valid_obs(proj_ent) if proj_ent else []




    today = now_iso()[:10]
    writes_today = 0
    recent_buf = deque(maxlen=_SS_CAP_EVENTS)
    total_events = 0
    for line in ev_lines:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        ep = ev.get("project")
        if isinstance(ep, str) and _slug_in_family(ep, fam):
            total_events += 1
            recent_buf.append(ev)
            if (str(ev.get("ts", ""))[:10] == today
                    and ev.get("action") in _WRITE_ACTIONS):
                writes_today += 1

    title = (proj_ent or {}).get("title") or project
    header = f"# Проект: {project}" + (f" — {title}" if title != project else "")
    totals = {"project": len(proj_facts), "decisions": 0, "bugs": 0,
              "tasks": 0, "sessions": 0, "events": total_events,
              "unclosed": 0}
    sections = []
    sec_names = []

    if not proj_ents and not proj_facts and not total_events:
        notice = (f"Проект «{project}» новый или пуст: в памяти по этому slug пока ничего нет. "
                  f'Начни с create_entities(project="{project}") — сущность проекта создастся '
                  "автоматически, факты добавляй через add_observations. Этот блок наполнится "
                  "после первых записей.")
        totals["project"] = 1
        sections.append(("project", header, [notice]))
        sec_names.append([""])
    else:
        plines = [f"- {f}" for f in proj_facts[:_SS_CAP_PROJECT_FACTS]]
        sections.append(("project", header, plines))

        sec_names.append([proj_ent["name"]] * len(plines))



        decisions = _ss_sort_recent(
            (e for e in proj_ents if e.get("entityType") == "decision"), mult_fn)
        dlines, dnames = [], []
        for e in decisions:
            obs_valid = _valid_obs(e)
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
            obs_valid = _valid_obs(e)
            totals["bugs"] += max(1, len(obs_valid))
            head = _ss_head(e)
            if not obs_valid:
                if len(blines) < _SS_CAP_BUG_LINES:
                    blines.append(head)
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



        tasks = _ss_sort_recent(
            (e for e in proj_ents
             if e.get("entityType") == "task" and _task_is_open(e)), mult_fn)
        totals["tasks"] = len(tasks)
        tlines, tnames = [], []
        for e in tasks[:_SS_CAP_TASK_LINES]:
            line = _ss_head(e)
            obs_valid = _valid_obs(e)
            if obs_valid:
                line += ": " + str(obs_valid[0])
            tlines.append(line)
            tnames.append(e["name"])
        if tlines:
            sections.append(("tasks", "## Открытые задачи", tlines))
            sec_names.append(tnames)





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
                sec_names.append([""])
                totals["unclosed"] = 1



        sessions = sorted((e for e in proj_ents if e.get("entityType") == "session"),
                          key=_recency_key, reverse=True)
        totals["sessions"] = len(sessions)
        slines, snames = [], []
        for e in sessions[:_SS_CAP_SESSIONS]:
            line = _ss_head(e, "name")
            facts2 = [str(o) for o in _valid_obs(e)[:2]]
            if facts2:
                line += ": " + "; ".join(facts2)
            slines.append(line)
            snames.append(e["name"])
        if slines:
            sections.append(("sessions", "## Прошлые сессии", slines))
            sec_names.append(snames)

        elines = []
        for ev in reversed(recent_buf):
            ts = str(ev.get("ts", ""))[:16].replace("T", " ")
            line = f"- {ts} {ev.get('action', '')}".rstrip()
            ent = str(ev.get("entity") or "")
            if ent:
                line += f" {ent}"
            det = str(ev.get("detail") or "")



            if det and ev.get("action") != "add_observation":
                line += f" — {det}"
            elines.append(line)
        if elines:
            sections.append(("events", "## Недавние события", elines))
            sec_names.append([""] * len(elines))

    text, shown, truncated = _fit_budget(sections, budget)



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
    if aliased_to:
        meta["aliased_to"] = aliased_to
    log.info("session_start: project=%s budget=%d used=%d truncated=%s",
             project, budget, len(text), truncated)
    return {"context_block": text, "meta": meta}




class EntityIn(BaseModel):
    name: str
    entityType: str = "general"
    title: str = ""
    tags: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    status: str = ""


class RelationIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    relationType: str


class ObservationsIn(BaseModel):
    entityName: str
    contents: list[str] = Field(default_factory=list)

    supersedes: list[str] = Field(default_factory=list)


class DeletionIn(BaseModel):
    entityName: str
    observations: list[str] = Field(default_factory=list)


class ObsIdDeletionIn(BaseModel):

    entity: str
    ids: list[str] = Field(default_factory=list)




def _license_status() -> dict:

    try:
        st = licensing.status(DATA_DIR, LICENSE_SECRET)
        latest = licensing.latest_version(DATA_DIR, LICENSE_SECRET)
        st["latest"] = latest
        st["update_available"] = licensing.version_newer(latest, VERSION)
        st["fp_class"] = hostfp.fp_class()
        return st
    except Exception as e:
        log.error("licensing.status failed: %s", e)
        return {"state": "expired", "days_left": 0, "reason": f"status error: {e}"}


def _write_block():





    st = _license_status()
    if licensing.allows_write(st):
        return None
    log.warning("write blocked: license state=%s (%s)", st.get("state"), st.get("reason", ""))
    return {"error": "Trial expired — activate license: /#activate", "activateUrl": "/#activate",
            "license": st.get("state")}


def _readonly_guard():







    if not READONLY:
        return None
    return {"ok": False, "error": "readonly_mirror"}






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



try:
    mcp._mcp_server.version = VERSION
except Exception:
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
    """Create multiple new entities in the knowledge graph. name = kebab-case id навсегда (не меняется), title = человеческое имя. Сначала search_nodes — не дублируй существующие. entityType: канонические project/session/bug/decision/request/event/task (синонимы RU/EN канонизируются: bugfix→bug, решение→decision, просьба→request, задача→task; свободный тип сохраняется как есть); type=project зарезервирован для проектов-рабочих-папок (система создаёт сама) — для продукта/сервиса/темы внутри проекта используй свободный тип (product/service/topic/…), иначе сущность будет отклонена; bug получает status open/fixed, task — open/done (default open). project: kebab-slug проекта (кириллица нормализуется в транслит) — сущность project:<slug> создаётся автоматически, существующее имя в чужом проекте → warning без merge."""
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
    """Add new observations to existing entities. Одно наблюдение = один атомарный факт (с датой, если важно). Пиши СРАЗУ, как узнал, — не в конце разговора. Факт УСТАРЕЛ (цена, версия, решение) — не удаляй и не дублируй: передай старую строку в supersedes того же item (пометится [~дата] и уйдёт из поиска/session_start, оставшись в архиве; вернуть — update_entity(restore=[...]); физически удалить можно всегда — delete_observations). Ненайденные строки — в supersedesNotFound ответа. project: kebab-slug — сущности чужого проекта считаются ненайденными."""
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
                 limit: int = 20, with_ids: bool = False) -> dict:
    """Search for nodes by query. Используй вместо read_graph, когда нужна одна тема, и ПЕРЕД create_entities — чтобы не создать дубликат. project: kebab-slug — искать только в этом проекте; all_projects=true — явный кросс-поиск. type: фильтр по типу после нормализации (канонические: project/session/bug/decision/request/event/task; «bugfix»/«баг» → bug; свободный тип — точное совпадение). Непустой query — ранжированный FTS-lite: сортировка по релевантности, limit — топ-N (default 20), у сущностей ответа + score, snippet, highlights (позиции совпадений в snippet, без HTML); часто и недавно читаемые сущности дополнительно всплывают (ACT-R). Пустой query — все сущности с фильтрами, как раньше. Ответ включает канонический type, а для bug/task — status (open/fixed и open/done). with_ids=true: observations — объекты {id, text} для delete_observations_by_id; default false — старый формат строк."""
    slug, err = _check_project(project)
    if err:
        return {"error": err}
    return op_search(query, slug, all_projects, type, limit, with_ids=with_ids)


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
    """ВЫЗЫВАЙ ПЕРВЫМ на старте сессии: бюджетный блок контекста проекта — {context_block, meta}. context_block готов к вставке в промпт: # Проект → ## Решения владельца (decision, дословно) → ## Открытые баги (bug, не закрытые) → ## Открытые задачи (task, status≠done — дела на потом) → ## Прошлые сессии (session, имя + 2 факта) → ## Недавние события. Внутри решений/багов/задач порядок — актуальность: свежесть × частота обращений (ACT-R). Жёсткий бюджет budget_chars (default 10000): блок никогда его не превышает, усечение построчное с маркером «… (ещё N, усечено бюджетом)», флаг meta.truncated. project ОБЯЗАТЕЛЕН (kebab-slug) — без него весь граф намеренно не читается."""
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




_PAGES = {"landing.html": DATA_DIR / "landing.html",
          "billing.html": DATA_DIR / "billing.html",
          "privacy.html": DATA_DIR / "privacy.html", "offer.html": DATA_DIR / "offer.html"}







def _page_html(name: str) -> str:
    override = _PAGES.get(name)
    if override is not None and override.exists():
        return override.read_text(encoding="utf-8")
    return (_RESOURCE_BASE / name).read_text(encoding="utf-8")


def _dashboard_html() -> str:
    return _page_html("dashboard.html")






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

    slug, err = _query_project(request)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    slug = _resolve_project(slug)
    with _lock:
        ents, rels = _load()
        events_raw = EVENTS_FILE.read_text(encoding="utf-8") if EVENTS_FILE.exists() else ""
    es, rs = (ents, rels) if not slug else _filter_graph(ents, rels, slug, False)
    memory_raw = "".join(json.dumps({"type": "entity", **e}, ensure_ascii=False) + "\n"
                         for e in (es.values() if isinstance(es, dict) else es))
    memory_raw += "".join(json.dumps({"type": "relation", **r}, ensure_ascii=False) + "\n" for r in rs)
    if slug:
        lines = []
        for line in events_raw.splitlines():
            try:
                if _canon_slug(json.loads(line).get("project") or "") == _canon_slug(slug):
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


def _project_memory_counts() -> dict:





    ents, _ = _load()
    counts = {}
    for e in ents.values():
        slug = _canon_slug(e.get("project") or "")
        if not slug or e["name"].startswith("project:"):
            continue
        if e.get("entityType") == "session":
            continue
        counts[slug] = counts.get(slug, 0) + 1
        canon = _resolve_project(slug)
        if canon != slug:
            counts[canon] = counts.get(canon, 0) + 1
    return counts


@mcp.custom_route("/api/search", methods=["GET"])
async def api_search(request: Request):



    slug, err = _query_project(request)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    try:
        limit = max(1, min(int(request.query_params.get("limit", "") or 0)
                           or searchrank.DEFAULT_LIMIT, 500))
    except (TypeError, ValueError):
        limit = searchrank.DEFAULT_LIMIT
    import asyncio



    resp = await asyncio.to_thread(
        op_search, request.query_params.get("q", ""), slug,
        _truthy(request.query_params.get("all_projects")),
        request.query_params.get("type", ""), limit)
    if (not request.query_params.get("q")
            and request.query_params.get("type") == "project"):
        resp = dict(resp)
        resp["memory_counts"] = await asyncio.to_thread(_project_memory_counts)



        if isinstance(resp.get("entities"), list):
            seen, collapsed = set(), []
            for e in resp["entities"]:
                name = e.get("name") or ""
                s = name[8:] if name.startswith("project:") else name





                if not name.startswith("project:"):
                    ep = _canon_slug(e.get("project") or "")
                    if ep and ep != _canon_slug(s):
                        continue

                canon = _canon_slug(_resolve_project(s))


                if canon in seen:
                    continue
                seen.add(canon)
                if canon != s:
                    e = dict(e, name=f"project:{canon}", title=canon)
                collapsed.append(e)
            resp["entities"] = collapsed
    return JSONResponse(resp)







RECALL_MARKER = "▣"

RECALL_MAX_BODY = 65536
RECALL_MAX_QUERY = 500
RECALL_MAX_FACT = 300
RECALL_MAX_FACTS_CHARS = 1500
RECALL_TOP_ENTITIES = 3
RECALL_MIN_SCORE = float(os.environ.get("MG_RECALL_MIN_SCORE", "0.85"))
RECALL_MIN_COS = float(os.environ.get("MG_RECALL_MIN_COS", "0.82"))
RECALL_RATE_LIMIT = 120
SESSION_CONTEXT_DEFAULT_BUDGET = 4000


@mcp.custom_route("/api/session-context", methods=["GET"])
async def api_session_context(request: Request):








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



    return JSONResponse(await asyncio.to_thread(op_session_start, slug, budget,
                                                False),
                        headers=_NOCACHE)


@mcp.custom_route("/api/recall", methods=["POST"])
async def api_recall(request: Request):












    ip = _client_ip(request)
    if _rate_limited(ip, "recall", RECALL_RATE_LIMIT, 60):
        return JSONResponse({"error": "too_many_requests"}, status_code=429,
                            headers={"Retry-After": "60"})



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


    return JSONResponse(await asyncio.to_thread(_recall, query, slug),
                        headers=_NOCACHE)


def _recall(query: str, slug: str) -> dict:

    slug = _resolve_project(slug)
    res = op_search(query, slug, limit=10)
    qts = set(searchrank.tokenize(query))






    _w, cos_fn = _hybrid_search(query)
    facts, total = [], 0
    for e in res.get("entities", []):
        if len(facts) >= RECALL_TOP_ENTITIES:
            break
        if cos_fn is not None:
            try:
                cos = cos_fn(e["name"])
            except Exception as exc:
                cos = None
                log.debug("recall: cos_fn сбой для %s: %s", e.get("name"), exc)
            if cos is not None and cos < RECALL_MIN_COS:
                continue
        score = float(e.get("score") or 0)
        if score < RECALL_MIN_SCORE:
            continue
        obs = [str(o).strip() for o in e.get("observations", [])
               if str(o).strip() and not _is_invalidated(o)]
        if not obs:
            continue


        best = sorted(range(len(obs)),
                      key=lambda i: (-sum(1 for t in qts if t in obs[i].lower()), -i))
        text = " | ".join(obs[i][:RECALL_MAX_FACT] for i in best[:2])
        if total + len(text) > RECALL_MAX_FACTS_CHARS:
            break
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
                                    _truthy(request.query_params.get("all_projects")),
                                    tools=_truthy(request.query_params.get("tools"))))


def _embed_detail():



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
        "fileBytes": _graph_file_bytes(),
        "license": {"state": lic.get("state"), "daysLeft": lic.get("days_left")},
        "embed": EMBED_ON,
        "embedDetail": _embed_detail(),
    }
    if slug:
        out["project"] = slug
    return JSONResponse(out)


def _body_project(body) -> tuple[str, str]:

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
    raw, rerr = await _read_capped(request, 262144)
    if rerr is not None:
        return JSONResponse({"error": "bad request"}, status_code=400)
    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    rest = body.get("restore")
    if rest is not None and not isinstance(rest, (list, str)):
        return JSONResponse({"error": "restore must be an array of strings"},
                            status_code=400)
    return JSONResponse(op_update_entity(body, slug))




@mcp.custom_route("/api/task/add", methods=["POST"])
async def api_task_add(request: Request):
    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    raw, rerr = await _read_capped(request, 262144)
    if rerr is not None:
        return JSONResponse({"error": "bad request"}, status_code=400)
    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    slug, err = _body_project(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    links = body.get("links")
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

    slug, err = _query_project(request)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse(op_task_list(slug), headers=_NOCACHE)


@mcp.custom_route("/api/session/close", methods=["POST"])
async def api_session_close(request: Request):

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
    raw, rerr = await _read_capped(request, 262144)
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
    for it in items:
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


@mcp.custom_route("/api/project/delete", methods=["POST"])
async def api_project_delete(request: Request):


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



    ro = _readonly_guard()
    if ro:
        return JSONResponse(ro, status_code=403)
    blocked = _write_block()
    if blocked:
        return JSONResponse(blocked, status_code=402)
    body_raw, rerr = await _read_capped(request, 262144)
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








HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd")
INGEST_MAX_EVENTS = 100
INGEST_MAX_BODY = 1_000_000
INGEST_RATE_LIMIT = 60
INGEST_HASHES_FILE = DATA_DIR / "ingest-hashes.json"
INGEST_HASHES_KEEP = 1000


def _ingest_hash(ev: dict) -> str:



    canon = json.dumps({"hook": ev.get("hook", ""), "project": ev.get("project", ""),
                        "ts": ev.get("ts", ""), "data": ev.get("data", {})},
                       sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _ingest_hashes_load() -> list:
    try:
        if INGEST_HASHES_FILE.exists():
            data = json.loads(INGEST_HASHES_FILE.read_text(encoding="utf-8"))
            return [h for h in data.get("hashes", []) if isinstance(h, str)]
    except Exception as ex:
        log.warning("ingest-hashes: сбой чтения: %s", ex)
    return []


def _ingest_hashes_save(hashes: list):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = INGEST_HASHES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"hashes": hashes[-INGEST_HASHES_KEEP:]},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(INGEST_HASHES_FILE)
    except Exception as ex:
        log.warning("ingest-hashes: сбой записи: %s", ex)







_MACHINE_PROMPT_RE = re.compile(
    r"^(?:Agent [0-9a-f-]{36} sent a message:|Background subagent [0-9a-f-]{36} |"
    r"background job \S+ |# MaxGraph: память проекта|Проект этой папки: |"
    r"Current runtime context\.|"
    r"This is an automatically generated checkpoint|Time sampled while preparing|"
    r"<system-reminder>)")


def _hook_digest(hook: str, data: dict) -> str:



    def short(v, n):
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        return re.sub(r"\s+", " ", str(v or "")).strip()[:n]

    def key_arg(inp):



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
            obs = inp.get("observations")
            if isinstance(obs, list) and obs and isinstance(obs[0], dict):
                return short(obs[0].get("entityName"), 100)
            names = inp.get("names")
            if isinstance(names, list) and names:
                return short(", ".join(str(x) for x in names[:3]), 100)
            return ""
        return short(inp, 100)

    def resp_text(r):

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
    else:
        text = "стоп агента"
    return text[:200]


@mcp.custom_route("/api/ingest", methods=["POST"])
async def api_ingest(request: Request):







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
        slug = _resolve_project(slug)
        ts = ev.get("ts", "")
        if not isinstance(ts, str):
            return JSONResponse({"error": "event.ts must be a string"}, status_code=400)
        data = ev.get("data", {})
        if not isinstance(data, dict):
            return JSONResponse({"error": "event.data must be an object"}, status_code=400)





        legacy = []
        lp = ev.get("legacy_project")
        if isinstance(lp, str):
            lp = [lp]
        if isinstance(lp, list):
            for item in lp[:4]:
                lslug, lerr = _check_project(item)
                if not lerr and lslug and lslug != slug and lslug not in legacy:
                    legacy.append(lslug)


        host = str(ev.get("host") or "").strip().lower()[:32]
        if host and not re.fullmatch(r"[a-z0-9-]+", host):
            host = ""
        norm.append({"hook": hook, "project": slug, "ts": ts, "data": data,
                     "legacy_project": legacy, "host": host})

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











    legacy_hints = {}
    for ev in norm:
        for lp in ev.get("legacy_project") or []:
            if lp:
                legacy_hints.setdefault(lp, ev["project"])
    aliases_created = {}
    if legacy_hints:
        with _lock:
            ents, _rels = _load()
            aliases = dict(_aliases_load())
            for legacy, canon_tag in sorted(legacy_hints.items()):
                if not canon_tag:
                    continue


                cur = aliases.get(legacy)
                if cur == canon_tag:
                    continue
                if cur:




                    if any(_canon_slug(e.get("project") or "") == _canon_slug(cur) for e in ents.values()):
                        continue
                    log.warning("project-aliases: перезапись %s: %s → %s (пустой canon)",
                                legacy, cur, canon_tag)
                elif len(aliases) >= _ALIASES_CAP:
                    continue


                if _REPO_TAG_RE.match(legacy):
                    continue
                if canon_tag in aliases or legacy in aliases.values():



                    continue
                if _REPO_TAG_RE.match(canon_tag):



                    if not canon_tag.startswith(f"repo-{legacy}-"):
                        continue
                if any(_canon_slug(e.get("project") or "") == _canon_slug(canon_tag) for e in ents.values()):
                    continue
                if not any(_canon_slug(e.get("project") or "") == _canon_slug(legacy) for e in ents.values()):
                    continue
                aliases[legacy] = canon_tag
                aliases_created[legacy] = canon_tag
            if aliases_created:
                _aliases_save(aliases)
                log.info("project-aliases: созданы/перезаписаны %s", aliases_created)

    for ev in fresh:





        if ev["hook"] == "PostToolUse":
            continue


        if ev["hook"] == "UserPromptSubmit" and _MACHINE_PROMPT_RE.search(
                str(ev["data"].get("prompt") or "").strip()):
            continue
        _event(f"hook_{ev['hook']}", detail=_hook_digest(ev["hook"], ev["data"]),
               project=ev["project"], source="hook", host=ev.get("host") or "")



    path_targets = sorted({ev["project"] for ev in fresh
                           if ev["project"] and ev["data"].get("cwd")})










    ended = sorted({ev["project"] for ev in fresh if ev["hook"] == "SessionEnd"})
    all_slugs = sorted(set(ended) | set(path_targets))
    if all_slugs:
        today = now_iso()[:10]
        with _lock:
            ents, rels = _load()
            changed = False
            for slug in all_slugs:
                if slug in path_targets:
                    pent = ents.get(_project_entity_name(slug))
                    if pent is not None:
                        have = {str(o) for o in pent.get("observations", [])}
                        n_paths = sum(1 for o in have if o.startswith("путь: "))
                        slug_changed = False
                        for ev in fresh:
                            cwd_raw = ev["data"].get("cwd")


                            if not isinstance(cwd_raw, str):
                                continue
                            cwd = cwd_raw.strip()[:300]
                            if (ev["project"] != slug or not cwd
                                    or n_paths >= 20):
                                continue
                            fact = f"путь: {cwd}"
                            if fact not in have:
                                have.add(fact)
                                pent.setdefault("observations", []).append(fact)
                                n_paths += 1
                                changed = True
                                slug_changed = True
                        if slug_changed:
                            pent["updatedAt"] = now_iso()
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
                    }], project=slug, source="hook", mk_project=False)
                    changed = changed or ch






                sent = ents.get(name)
                if (sent is not None
                        and sent.get("entityType") == "session"
                        and _SESSION_CLOSE_TAG not in (sent.get("tags") or [])):
                    marker = "завершена без итогов агента (автозапись страховки)"


                    if not any(str(o).startswith(marker) for o in sent.get("observations", [])):
                        sent.setdefault("observations", []).append(
                            f"{marker}, {now_iso()[:10]}")
                        changed = True
                        _event("session_unclosed_marker",
                               detail=f"SessionEnd без session_close: {name}",
                               project=slug, source="hook")




                want = []
                for ev in fresh:
                    if ev["hook"] == "SessionEnd" and ev["project"] == slug:
                        want.extend(_open_tasks_from_data(ev["data"]))
                if want:
                    have = {str(e.get("title") or "").strip().lower()
                            for e in ents.values()
                            if _canon_slug(e.get("project") or "") == _canon_slug(slug)
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
                                                          project=slug, source="hook",
                                                          mk_project=False)
                        changed = changed or ch
            if changed:
                _save(ents, rels)
    out = {"ok": True, "accepted": len(fresh), "dedup": dedup}
    if aliases_created:
        out["aliases"] = aliases_created
    return JSONResponse(out)




CODES_FILE = DATA_DIR / "codes.json"
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_TIERS = {"pro": {"price": "от 149₽/мес", "label": "Pro"}}
_PAID_STATUSES = ("success", "paid", "completed", "confirmed", "successfully")
_REDEEMABLE_STATUSES = ("new", "sold")
def _plan_for(amount: float, currency: str) -> str:



    cur = currency.upper() if currency else "RUB"
    best = "pro1"
    for pid in sorted(PLANS, key=lambda p: PLANS[p].get(cur, PLANS[p]["RUB"])):
        if PLANS[pid].get("donation"):
            continue
        if amount >= PLANS[pid].get(cur, PLANS[pid]["RUB"]):
            best = pid
    return best
_codes_lock = threading.Lock()
_RECOVERY_COOLDOWN = 3600


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
        data["payments"] = data.get("payments", [])[-500:]


        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CODES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(CODES_FILE)
        try:
            os.chmod(CODES_FILE, 0o600)
        except OSError:
            pass


def _new_code() -> str:
    import secrets

    def group():
        return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))

    return f"MG-{group()}-{group()}-{group()}"






_CORS = {"Access-Control-Allow-Origin": "*",
         "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
         "Access-Control-Allow-Headers": "Content-Type",
         "Access-Control-Max-Age": "86400"}


def _key_hash(key: str) -> str:

    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _is_revoked(data: dict, key: str) -> dict | None:

    h = _key_hash(key)
    for r in data.get("revoked", []):
        if hmac.compare_digest(str(r.get("h", "")), h):
            return r
    return None


_REVOKED_MAX = 500


def _add_revoked(data: dict, key: str, note: str = "") -> None:


    rev = data.setdefault("revoked", [])
    h = _key_hash(key)
    if not any(hmac.compare_digest(str(r.get("h", "")), h) for r in rev):
        rev.append({"h": h, "ts": time.time(), "note": (note or "")[:254]})
    if len(rev) > _REVOKED_MAX:
        data["revoked"] = rev[-_REVOKED_MAX:]




_installs_lock = threading.Lock()
_INSTALL_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_FP_RE = re.compile(r"^[0-9a-f]{64}$")

_INSTALL_FLAVORS = frozenset({"docker", "docker-desktop", "rancher", "python", "native"})
_INSTALLS_CAP = 10000
_TRIALS_CAP = 100_000
_FP_KINDS = frozenset({"composite", "legacy", "container"})


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")


def _sanitize_email(value) -> str:

    em = str(value or "").strip().lower()[:254]
    return em if _EMAIL_RE.match(em) else ""


def _parse_hostfp_body(body: dict) -> tuple:






    hf = str(body.get("host_fp") or "").strip().lower()
    if not _FP_RE.match(hf or ""):
        hf = ""
    components = hostfp.sanitize_components(body.get("components")) if hf else {}
    kind = str(body.get("fp_kind") or "").strip().lower()
    if kind not in _FP_KINDS:
        kind = "composite" if hf else ""
    if kind == "composite" and not hf:
        kind = ""
    return hf, components, kind


def _adopt_trial_anchor(trials: dict, eff: str, prev_fp: str,
                        components: dict, allow_scoring: bool = True):











    def _claim(old_key, verdict):
        t_old = trials[old_key]
        t = dict(t_old)
        t["installs"] = list(t.get("installs") or [])
        t.pop("ghost", None)
        t.pop("ghost_since", None)
        t.pop("volatile", None)
        t.pop("last_seen", None)
        if components and not t.get("components"):
            t["components"] = components
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


    if not INSTALLS_FILE.exists():
        return {"installs": {}}
    try:
        data = json.loads(INSTALLS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("installs", {})
            return data
    except Exception:
        pass
    return None


def _save_installs(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INSTALLS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(INSTALLS_FILE)


_ghost_gc_day = ""
_GHOST_TTL_SEC = 90 * 86400
_VOLATILE_TTL_SEC = 7 * 86400



def _gc_ghost_fps(data: dict, now: float) -> tuple:










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
                provable = False
                break
            lives.add(cur)
        if provable:
            t["ghost"] = True
            t["ghost_since"] = _safe_int(t.get("ghost_since"), now) or now
            marked.append((fp, sorted(lives)))
    return marked, deleted, volatile_deleted


def _rebind_ghost_licenses(ghosts) -> int:






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
                if new != fps:
                    rec["fps"] = new
                    changed = True
                al = rec.get("aliases") or []
                if al:
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







MACHINES_FILE = DATA_DIR / "machines.json"
_machines_lock = threading.Lock()
_MACHINES_MAX_FP = 1
_MACHINES_CAP = 10000


_machines_reset_notified = False


def _load_machines() -> dict:





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
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(MACHINES_FILE)
    try:
        os.chmod(MACHINES_FILE, 0o600)
    except OSError:
        pass


def _machine_bind(data: dict, key: str, fp: str, now: float,
                  components: dict | None = None, legacy_fp: str = "",
                  email: str = "") -> tuple:
















    h = _key_hash(key)
    rec = data["machines"].get(h)
    em = _sanitize_email(email)
    if rec is not None and em:
        rec["email"] = em
    fps = (rec or {}).get("fps") or []
    aliases = (rec or {}).get("aliases") or []
    if rec and fp in fps:
        rec["updated"] = now
        if components and not rec.get("components"):
            rec["components"] = components
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



        linked = (legacy_fp and not rec.get("components")
                  and (legacy_fp in fps or legacy_fp in (rec.get("aliases") or [])))
        if linked:
            verdict = "rebound"
        elif components and rec.get("components"):
            v, _pct = hostfp.score(rec["components"], components)
            if v == "new":
                return "limit", rec
            verdict = "rebound" if v == "same" else "review"
        else:
            return "limit", rec
        for old in fps:
            if old != fp and old not in aliases:
                aliases.append(old)
        del aliases[:-8]
        rec["aliases"] = aliases
        fps[:] = []
    fps.append(fp)
    if components and not rec.get("components"):
        rec["components"] = components

    if verdict == "review":
        rec["review"] = True
    else:
        rec.pop("review", None)
    rec["updated"] = now
    if len(data["machines"]) > _MACHINES_CAP:
        stale = sorted(data["machines"], key=lambda k: data["machines"][k].get("updated", 0))
        for k in stale[: len(data["machines"]) - _MACHINES_CAP]:
            del data["machines"][k]
    return verdict, rec


_MACHINE_LIMIT_MSG = ("Ключ уже привязан к другому компьютеру. Отвязка — только через "
                      "владельца магазина: help@rezon-ai.ru")


def _install_id(data_dir=None) -> str:

    f = (data_dir or DATA_DIR) / "install.json"
    try:
        if f.exists():
            iid = json.loads(f.read_text(encoding="utf-8")).get("install_id") or ""
            if iid:
                return iid
        iid = str(uuid.uuid4())
        f.write_text(json.dumps({"install_id": iid, "created": now_iso()}), encoding="utf-8")
        return iid
    except (OSError, ValueError):
        return str(uuid.uuid4())


class _NoRedirectPost(urllib.request.HTTPRedirectHandler):



    def redirect_request(self, *a, **k):
        return None


_NO_REDIRECT_POST_OPENER = urllib.request.build_opener(_NoRedirectPost)


def _post_json(url: str, payload: dict, timeout: int = 5):

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    raw = _NO_REDIRECT_POST_OPENER.open(req, timeout=timeout).read(65536)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _get_json(url: str, timeout: int = 5):


    raw = urllib.request.urlopen(url, timeout=timeout).read(65536)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _client_fp_fields() -> dict:









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






    env = env if env is not None else os.environ
    if STORE_MODE:
        return
    payload = {"install_id": _install_id(data_dir), "version": VERSION,



               "install_flavor": (env.get("INSTALL_FLAVOR") or "").strip()
               or ("native" if _FROZEN else "docker" if os.path.exists("/.dockerenv") else "python")}
    payload.update(_client_fp_fields())
    try:
        resp = (post or _post_json)(PUBLIC_BASE_URL + "/api/telemetry/install", payload, timeout=5)
    except Exception:
        return
    try:
        ts = (resp or {}).get("trial", {}).get("started_at")
        if ts is not None:
            dd = data_dir or DATA_DIR
            licensing.anchor_trial_start(dd, licensing.get_secret(dd), ts)
    except Exception:
        pass


def _bootstrap_hosted():





    _telemetry_ping()
    _relay_config_pull()


def _relay_config_pull(env=None):










    env = env if env is not None else os.environ
    if (env.get("EMBED_URL") or "").strip():
        return
    try:
        key = licensing.current_key(DATA_DIR, LICENSE_SECRET)
        if key:
            payload = {"key": key}
        else:
            if licensing.status(DATA_DIR, LICENSE_SECRET).get("state") != "trial":
                return
            fields = _client_fp_fields()
            if not fields.get("fp") and not fields.get("host_fp"):
                return
            payload = {"install_id": _install_id(), **fields}
        cfg = _post_json(PUBLIC_BASE_URL + "/api/relay/config", payload, timeout=8)
        ok = isinstance(cfg, dict) and cfg.get("ok")
        if not ok:
            err = cfg.get("error") if isinstance(cfg, dict) else None
            log.warning("relay: конфиг не получен (%r)", str(err or "магазин недоступен")[:120])
        base = (PUBLIC_BASE_URL or "").rstrip("/")




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
                _ensure_embed_loop()
                log.info("relay: embed-конфиг применён (model=%.80r)",
                         emb.get("model") or "default")


        rank = ok and cfg.get("rank")
        if isinstance(rank, dict) and rankcfg.apply_rank_config(rank, path=RANK_CONFIG_FILE):
            log.info("relay: rank-конфиг применён (version=%s, синонимов=%d)",
                     rank.get("version"), len(rank.get("synonyms") or {}))
    except Exception as e:

        log.warning("relay: конфиг не получен: %r", e)


def _watchdog_iteration(env):






    try:
        v = _get_json(PUBLIC_BASE_URL + "/api/version", timeout=8)
        if isinstance(v, dict) and v.get("version"):
            licensing.set_latest(DATA_DIR, LICENSE_SECRET, str(v["version"]))
    except Exception:
        pass
    key = licensing.current_key(DATA_DIR, LICENSE_SECRET)
    if key:
        payload = {"key": key}
        payload.update(_client_fp_fields())

        payload["install_id"] = _install_id()
        try:
            resp = _post_json(PUBLIC_BASE_URL + "/api/license/validate", payload, timeout=8)
        except Exception as e:
            resp = _http_error_json(e)
        if isinstance(resp, dict):
            if resp.get("revoked"):
                if licensing.set_revoked(DATA_DIR, LICENSE_SECRET, resp.get("revoked_at") or time.time()):
                    _event("license_revoked", "", "магазин подтвердил отзыв; grace 72ч")
                    log.warning("license revoked по магазину — grace 72ч до блокировки записи")
            elif resp.get("ok"):
                if licensing.set_revoked(DATA_DIR, LICENSE_SECRET, None):
                    _event("license_unrevoked", "", "магазин снял отзыв")
                _relay_config_pull(env)
            elif resp.get("error") == "machine_limit":


                log.warning("watchdog: магазин ответил machine_limit — лицензия сохранена")


def _license_watchdog(env=None):








    env = env if env is not None else os.environ
    if STORE_MODE:
        return
    time.sleep(300)
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
        rec["days"] = days
    return rec


def _license_for_code(data: dict, code: str):


    rec = next((c for c in data["codes"] if c.get("code") == code), None)
    if rec and rec.get("licenseKey"):
        return rec["licenseKey"], rec
    pay = next((p for p in data.get("payments", [])
                if p.get("code") == code and p.get("licenseKey")), None)
    return (pay or {}).get("licenseKey"), rec




def _token_hash(token: str) -> str:

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def migrate_token_hashes() -> int:





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





TRUST_PROXY = (os.environ.get("TRUST_PROXY") or "").strip() == "1"


def _client_ip(request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:

            return xff.split(",")[-1].strip()
    return request.client.host if request.client else "?"


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _read_capped(request: Request, limit: int) -> "tuple[bytes | None, Response | None]":






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
        request._body = body
        return body, None
    body = await request.body()
    if len(body) > limit:
        return None, JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    return body, None


def _valid_email(value) -> str | None:




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


    if not STORE_MODE:
        return Response(status_code=302, headers={"location": "/app"})
    return await _landing_page(request)


@mcp.custom_route("/landing", methods=["GET"])
async def landing(request: Request):

    if STORE_MODE:
        return PlainTextResponse("Not Found", status_code=404)
    try:
        return await _landing_page(request)
    except FileNotFoundError:

        return PlainTextResponse("Not Found", status_code=404)


async def _landing_page(request: Request):
    html = _page_html("landing.html")



    flag = {"store": STORE_MODE, "storeUrl": PUBLIC_BASE_URL}
    html = html.replace("</head>", f"<script>window.__MG_STORE__={json.dumps(flag)};</script></head>", 1)
    headers = dict(_NOCACHE)
    if STORE_MODE:

        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "img-src 'self' data:", "img-src 'self' data: https://counter.yadro.ru")
    else:


        _li_a = html.find("<!--LiveInternet counter-->")
        _li_b = html.find("<!--/LiveInternet-->")
        if _li_a != -1 and _li_b != -1:
            html = html[:_li_a] + html[_li_b + 20:]



    code = _ref_norm(request.query_params.get("ref"))
    if code:
        with _ref_lock:
            refs = _load_referrals()
            rec = refs.get(code) if refs is not None else None
            if rec is not None:


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






    if not STORE_MODE:
        return PlainTextResponse("Not Found", status_code=404)
    if not _admin_auth(request):
        return HTMLResponse(_ADMIN_GATE_HTML, headers=_NOCACHE)
    return HTMLResponse(_page_html("admin.html"), headers=_NOCACHE)


def _store_gate():




    return None if STORE_MODE else PlainTextResponse("Not Found", status_code=404)


_OG_FILE = _RESOURCE_BASE / "og.png"


_FONTS_DIR = _RESOURCE_BASE / "static" / "fonts"
_FONT_RE = re.compile(r"[a-z0-9-]+\.woff2")


@mcp.custom_route("/.well-known/security.txt", methods=["GET"])
async def security_txt(request: Request):

    return PlainTextResponse("Contact: mailto:help@rezon-ai.ru\n"
                             "Expires: 2027-12-31T23:59:59Z\n"
                             "Preferred-Languages: ru, en\n",
                             media_type="text/plain", headers={"Cache-Control": "public, max-age=86400"})


@mcp.custom_route("/static/fonts/{fname}", methods=["GET"])
async def static_font(request: Request):


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

    if not _OG_FILE.exists():
        return PlainTextResponse("Not Found", status_code=404)
    return Response(_OG_FILE.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


_FAVICON_FILE = _RESOURCE_BASE / "favicon.ico"


@mcp.custom_route("/favicon.ico", methods=["GET"])
async def favicon(request: Request):


    if not _FAVICON_FILE.exists():
        return PlainTextResponse("Not Found", status_code=404)
    return Response(_FAVICON_FILE.read_bytes(), media_type="image/x-icon",
                    headers={"Cache-Control": "public, max-age=86400"})


_DEMO_SHOT_LOCAL = _RESOURCE_BASE / "static" / "demo-dashboard-dark.png"
_DEMO_SHOT_DATA = DATA_DIR / "static" / "demo-dashboard-dark.png"


@mcp.custom_route("/static/demo-dashboard-dark.png", methods=["GET"])
async def demo_shot(request: Request):

    f = _DEMO_SHOT_DATA if _DEMO_SHOT_DATA.exists() else _DEMO_SHOT_LOCAL
    if not f.exists():
        return PlainTextResponse("Not Found", status_code=404)
    return Response(f.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


def _page_or_404(name: str):

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
    body, err = await _read_capped(request, 4096)
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







    ip = _client_ip(request)
    if _rate_limited(ip, "telemetry", 10, 3600):
        return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)

    body, err = await _read_capped(request, 4096)
    if err is not None:
        return JSONResponse({"ok": False, "error": "too_large"}, status_code=413)
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    iid = str(body.get("install_id") or "")
    ver = str(body.get("version") or "")
    if not _INSTALL_ID_RE.match(iid) or not ver or len(ver) > 24:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    fp = str(body.get("fp") or "")
    if fp and not _FP_RE.match(fp):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    host_fp, components, fp_kind = _parse_hostfp_body(body)
    eff = host_fp or fp
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
        if data is None:
            return JSONResponse({"ok": True}, headers=_NOCACHE)
        rec = data["installs"].get(iid)
        prev_fp = str((rec or {}).get("fp") or "")
        first = rec is None
        rec = rec or {"first_seen": now, "starts": 0}
        rec.update({"last_seen": now, "version": ver, "starts": int(rec.get("starts") or 0) + 1})
        if flavor in _INSTALL_FLAVORS:
            rec["install_flavor"] = flavor
        if eff:
            rec["fp"] = eff
            if fp_kind:
                rec["fp_kind"] = fp_kind
        data["installs"][iid] = rec
        if len(data["installs"]) > _INSTALLS_CAP:
            stale = sorted(data["installs"], key=lambda k: data["installs"][k].get("last_seen", 0))
            for k in stale[: len(data["installs"]) - _INSTALLS_CAP]:
                del data["installs"][k]
        if eff:
            trials = data.setdefault("trials", {})
            t = trials.get(eff)
            if t is not None and t.get("ghost"):

                t.pop("ghost", None)
                t.pop("ghost_since", None)
            if t is None:



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


                        t["volatile"] = True
                        t["last_seen"] = now
                    anchor_ip[akey] = _safe_int(anchor_ip.get(akey)) + 1
                else:
                    capped = True
                if _safe_int(anchor_ip.get(akey)):

                    data["anchor_ip"] = {k: v for k, v in anchor_ip.items()
                                         if k.endswith("|" + today)}
            if t is not None:
                seen = t.setdefault("installs", [])
                if iid not in seen:
                    t["reinstalls"] = int(t.get("reinstalls") or 0) + 1
                    if len(seen) < 100:
                        seen.append(iid)
                if components and not t.get("components"):
                    t["components"] = components
                if t.get("volatile"):
                    t["last_seen"] = now
                trial_ts = t.get("started_at")

        global _ghost_gc_day
        if _ghost_gc_day != today:
            _ghost_gc_day = today
            try:
                marked, deleted, vol_deleted = _gc_ghost_fps(data, now)
            except Exception:
                marked, deleted, vol_deleted = [], [], []
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


        _event("install_seen", iid, ver)
    out = {"ok": True}
    if trial_ts is not None:
        out["trial"] = {"started_at": trial_ts}
        out["trial_days"] = licensing.trial_days()
    return JSONResponse(out, headers=_NOCACHE)



_learn_lock = threading.Lock()
_learn_prune_day = ""
_LEARN_RETENTION_SEC = 90 * 86400
_LEARN_CAP = 500_000
_LEARN_BODY_CAP = 16384
_LEARN_MAX_EVENTS = 50
_LEARN_TEXT_MAX = 200
_LEARN_BYTES_CAP = 32 * 1024 * 1024



def _learn_clean(text):


    return (text.encode("utf-8", "ignore").decode("utf-8")
            .replace("\x00", "").strip()[:_LEARN_TEXT_MAX])


def _learn_prune(now):




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
        lines = []
    for line in lines:
        try:
            ts = float(json.loads(line).get("ts") or 0)
        except Exception:
            continue
        if ts >= cutoff:
            kept.append(line)
    kept = kept[-_LEARN_CAP:]
    try:
        tmp = LEARN_EVENTS_FILE.with_suffix(".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, LEARN_EVENTS_FILE)
    except OSError:
        pass
    _learn_prune_day = day


@mcp.custom_route("/api/telemetry/learn", methods=["POST"])
async def api_telemetry_learn(request: Request):




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
        try:
            full = (LEARN_EVENTS_FILE.exists()
                    and LEARN_EVENTS_FILE.stat().st_size > _LEARN_BYTES_CAP)
        except OSError:
            full = False
        if full:
            return JSONResponse({"ok": False, "error": "store_full"},
                                status_code=503)
        try:
            with LEARN_EVENTS_FILE.open("a", encoding="utf-8") as f:
                for ev in accepted:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:

            return JSONResponse({"ok": False, "error": "store_unavailable"},
                                status_code=503)
    return JSONResponse({"ok": True, "accepted": len(accepted)}, headers=_NOCACHE)



_hb_lock = threading.Lock()
_hb_prune_day = ""
_HB_RETENTION_SEC = 90 * 86400
_HB_BODY_CAP = 2048
_HB_BYTES_CAP = 8 * 1024 * 1024

_HB_OS = frozenset({"windows", "macos", "linux"})
_HB_ID_RE = re.compile(r"^[0-9A-Za-z-]{1,64}$")
_HB_VER_RE = re.compile(r"^\d+\.\d+\.\d+([-.+][0-9A-Za-z.-]+)?$")


def _hb_prune(now):




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
        lines = []
    for line in lines:
        try:
            ts = float(json.loads(line).get("ts") or 0)
        except Exception:
            continue
        if ts >= cutoff:
            kept.append(line)
    try:

        tmp = HEARTBEATS_FILE.with_name(f"heartbeats.{os.getpid()}.tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, HEARTBEATS_FILE)
    except OSError:
        pass
    _hb_prune_day = day


@mcp.custom_route("/api/telemetry/heartbeat", methods=["POST"])
async def api_telemetry_heartbeat(request: Request):




    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if _rate_limited(ip, "heartbeat", 10, 3600):
        return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)

    body, err = await _read_capped(request, _HB_BODY_CAP)
    if err is not None:
        return JSONResponse({"ok": False, "error": "too_large"}, status_code=413)
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    if not isinstance(body, dict):
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
        try:
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

            return JSONResponse({"ok": False, "error": "store_unavailable"},
                                status_code=503)
    return JSONResponse({"ok": True}, headers=_NOCACHE)



_sup_lock = threading.Lock()
_SUP_BODY_CAP = 16384
_SUP_BYTES_CAP = 2 * 1024 * 1024


@mcp.custom_route("/api/support/report", methods=["POST", "OPTIONS"])
async def api_support_report(request: Request):


    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS)
    resp = await _support_report_inner(request)
    resp.headers.update(_CORS)
    return resp


async def _support_report_inner(request: Request):





    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if _rate_limited(ip, "support", 5, 3600):
        return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)

    body, err = await _read_capped(request, _SUP_BODY_CAP)
    if err is not None:
        return JSONResponse({"ok": False, "error": "too_large"}, status_code=413)
    try:
        body = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    text = body.get("text")
    if not isinstance(text, str):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    text = text.strip()[:2000]
    if not text:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)

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
        try:
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

            return JSONResponse({"ok": False, "error": "store_unavailable"},
                                status_code=503)
    _notify_support_report(rec)
    return JSONResponse({"ok": True}, headers=_NOCACHE)



def _send_support_email(rec: dict):




    to_addr = (os.environ.get("OWNER_NOTIFY_EMAIL") or "").strip()
    if not to_addr or not os.environ.get("SMTP_HOST") or billing is None:
        log.debug("support email: пропуск — OWNER_NOTIFY_EMAIL/SMTP не настроены")
        return

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

        log.warning("support email failed: %s code=%s",
                    type(e).__name__, getattr(e, "smtp_code", None))


def _notify_support_report(rec: dict):


    threading.Thread(target=_send_support_email, args=(rec,),
                     daemon=True, name="support-email").start()



_SUP_LIST_CAP = 200


@mcp.custom_route("/api/admin/support-reports", methods=["GET"])
async def api_admin_support_reports(request: Request):




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
            except (OverflowError, ValueError):
                pass
        for name in ("version", "os", "install_id",
                     "update_log_tail", "server_log_errors"):
            v = rec.get(name)
            item[name] = v if isinstance(v, str) else ""
        out.append(item)
    return JSONResponse({"reports": out[-_SUP_LIST_CAP:][::-1]}, headers=_NOCACHE)


@mcp.custom_route("/api/support/context", methods=["GET"])
async def api_support_context(request: Request):



    tail, errs = "", []
    try:
        with UPDATE_LOG_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", "replace")[-4000:]
    except OSError:
        pass
    try:
        with SERVER_LOG_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 262144))
            lines = f.read().decode("utf-8", "replace").splitlines()
        errs = [l for l in lines if " ERROR " in l or " WARNING " in l][-10:]
    except OSError:
        pass
    osname = ("windows" if sys.platform.startswith("win")
              else ("macos" if sys.platform == "darwin" else "linux"))
    return JSONResponse({"version": VERSION, "os": osname,
                         "update_log_tail": tail, "server_log_errors": errs},
                        headers=_NOCACHE)



RANK_VETO_FILE = DATA_DIR / "rank_veto.json"
_rank_lock = threading.Lock()
_rank_cache = {"computed_at": 0.0, "mtime": -1.0,
               "cfg": {"version": 0, "synonyms": {}}, "stats": {}}
_RANK_RECOMPUTE_SEC = 3600
_RANK_MIN_COUNT = 5
_RANK_MIN_DAYS = 3
_RANK_NOISE_MIN = 2
_RANK_PAIRS_CAP = 500
_RANK_SYNONYMS_CAP = 5
_RANK_STATS_CAP = 200_000



def _load_rank_veto():

    try:
        v = json.loads(RANK_VETO_FILE.read_text(encoding="utf-8"))
        return {tuple(p) for p in v
                if isinstance(p, list) and len(p) == 2
                and all(isinstance(x, str) for x in p)}
    except Exception:
        return set()


def _surface_map(text):


    out = {}
    for _, _, raw in searchrank._subtokens(str(text or "")):
        out.setdefault(searchrank._stem(raw), {})
        out[searchrank._stem(raw)][raw] = out[searchrank._stem(raw)].get(raw, 0) + 1
    return out


def _rank_config_compute(now):





    try:
        mtime = LEARN_EVENTS_FILE.stat().st_mtime
    except OSError:
        return {"version": 0, "synonyms": {}}, {}


    try:
        vmtime = RANK_VETO_FILE.stat().st_mtime
    except OSError:
        vmtime = 0.0
    version = int(max(mtime, vmtime) * 1000)




    prev = _rank_cache["cfg"].get("version") or 0
    if version <= prev:
        version = prev + 1
    stats = {}
    try:
        lines = LEARN_EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError, ValueError):
        lines = []
    for line in lines:
        try:
            ev = json.loads(line)
            q, t = ev["q"], ev["t"]
        except Exception:
            continue
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
                        continue
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
    synonyms = {a: [max(v["surf_t"].items(), key=lambda kv: kv[1])[0]
                    for _, v in pairs]
                for a, pairs in by_q.items()}
    return {"version": version, "synonyms": synonyms}, stats


def _rank_config_cached():


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

    _rank_config_cached()
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

    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    return JSONResponse(_rank_admin_view(), headers=_NOCACHE)


@mcp.custom_route("/api/admin/synonyms/veto", methods=["POST"])
async def api_admin_synonyms_veto(request: Request):


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
        (a + b).encode("utf-8")
    except UnicodeEncodeError:
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
        _rank_cache["mtime"] = -1.0
        _rank_cache["computed_at"] = 0.0
    return JSONResponse({"ok": True}, headers=_NOCACHE)


@mcp.custom_route("/api/installs", methods=["GET"])
async def api_installs_list(request: Request):





    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    data = _load_installs() or {"installs": {}}
    installs = data["installs"]



    all_trials = data.get("trials") or {}
    trials = {k: v for k, v in all_trials.items()
              if not v.get("ghost") and not v.get("alias_of") and not v.get("volatile")}
    volatile_n = sum(1 for v in all_trials.values()
                     if isinstance(v, dict) and v.get("volatile") and not v.get("alias_of"))
    fp2kh = {}
    fp2mrec = {}
    try:
        for kh, rec in (_load_machines().get("machines") or {}).items():
            for fp in rec.get("fps") or []:
                fp2kh[fp] = kh
                fp2mrec[fp] = rec
    except Exception:
        pass
    kh2lic = {}
    try:
        codes = _load_codes()
        for p in codes.get("payments", []):
            k = p.get("licenseKey")
            if k:
                kh2lic[_key_hash(k)] = {"email": p.get("email") or "",
                                        "tier": p.get("plan") or "", "key": k,
                                        "revoked": bool(_is_revoked(codes, k))}
        for tn in codes.get("tenants", []):
            k = tn.get("licenseKey")
            if k:
                kh2lic[_key_hash(k)] = {"email": tn.get("email") or "",
                                        "tier": tn.get("tier") or "", "key": k,
                                        "revoked": bool(_is_revoked(codes, k))}
    except Exception:
        pass
    by_fp = {}
    unknown = []



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
                      key=lambda r: r.get("last_seen", 0), reverse=True)[:50]
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




    unique_installs = len(set(installs))
    return JSONResponse({"machines": machines[:500],
                         "unknown_installs": unknown[:500],
                         "kpi": {"machines": len(machines), "installs": unique_installs,
                                 "volatile": volatile_n}},
                        headers=_NOCACHE)


@mcp.custom_route("/api/payments", methods=["GET"])
async def api_payments_list(request: Request):


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

    ok = [p for p in out if p.get("status") not in _FAILED_STATUSES]
    failed = [p for p in out if p.get("status") in _FAILED_STATUSES]
    return JSONResponse({"payments": ok[:500], "failed": failed[:500]}, headers=_NOCACHE)


@mcp.custom_route("/api/admin/stats", methods=["GET"])
async def api_admin_stats(request: Request):






    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()

    def _ts(val) -> float:

        if isinstance(val, bool):
            return 0.0
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val)
                if dt.tzinfo is None:

                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except (ValueError, OverflowError):
                return 0.0
        return 0.0

    def _week_start(ts: float):
        d = datetime.fromtimestamp(ts, timezone.utc).date()
        return d - timedelta(days=d.weekday())

    data = _load_codes()
    now = time.time()
    today = datetime.fromtimestamp(now, timezone.utc).date()

    daily: dict = {}
    for p in data.get("payments", []):
        if p.get("status") in _FAILED_STATUSES:
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



_rate_buckets: dict = {}
_RATE_GC_AT = 50000



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
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    raw_code = body.get("code")
    code = (raw_code if isinstance(raw_code, str) else "")[:32].strip().upper()
    email = _valid_email(body.get("email"))
    recovery = None
    with _codes_lock:
        data = _load_codes()
        rec = next((c for c in data["codes"] if c["code"] == code), None)
        if not rec:
            return JSONResponse({"ok": False, "error": "Код не найден. Проверь написание."}, status_code=404)
        if rec["status"] == "activated":
            tenant = next((t for t in data["tenants"] if t["id"] == rec.get("tenant")), None)
            if not tenant:
                return JSONResponse({"ok": False, "error": "Код уже активирован."}, status_code=409)






            t_email = (tenant.get("email") or "").lower()
            if email and t_email and email == t_email:
                if os.environ.get("SMTP_HOST"):





                    last_rec = float(tenant.get("lastRecoveryAt") or 0)
                    if time.time() - last_rec < _RECOVERY_COOLDOWN:
                        return JSONResponse({"ok": False, "already": True,
                                             "error": "Восстановление — не чаще раза в час. "
                                                      "Письмо уже отправлено: проверь входящие и спам."},
                                            status_code=429, headers=_NOCACHE)
                    old_lic = tenant.get("licenseKey") or _license_for_code(data, code)[0]
                    exp = 0
                    if old_lic:
                        if _is_revoked(data, old_lic):


                            return JSONResponse({"ok": False, "already": True,
                                                 "error": "Лицензия отозвана. Вопросы — в поддержку: help@rezon-ai.ru"},
                                                status_code=409, headers=_NOCACHE)
                        payload, _e = licensing.verify_key(LICENSE_SECRET, old_lic, now=0)
                        if not payload:
                            return JSONResponse({"ok": False, "already": True,
                                                 "error": "Лицензия повреждена. Напиши в поддержку: help@rezon-ai.ru"},
                                                status_code=409, headers=_NOCACHE)
                        exp = int(payload.get("exp") or 0)
                    elif (rec.get("note") or "").strip().lower().startswith("trial"):

                        return JSONResponse({"ok": False, "already": True,
                                             "error": "Триальный код не несёт лицензии. Pro — после оплаты: rezon-ai.ru/#pricing"},
                                            status_code=409, headers=_NOCACHE)
                    new_lic = licensing.reissue_key(LICENSE_SECRET, t_email,
                                                    tenant.get("tier") or rec["tier"], exp)
                    if new_lic == old_lic:


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
            lic, _ = _license_for_code(data, code)
            if lic and _is_revoked(data, lic):

                return JSONResponse({"ok": False, "error": "Лицензия по этому коду отозвана. "
                                                           "Вопросы — в поддержку: help@rezon-ai.ru"},
                                    status_code=409, headers=_NOCACHE)
            if not lic:


                lic = licensing.generate_key(LICENSE_SECRET, email or "buyer@unknown",
                                             rec["tier"], _safe_int(rec.get("days"), 0))
            rec["licenseKey"] = lic
            tenant["licenseKey"] = lic
            tenant["paidAt"] = now_iso()
            data["tenants"].append(tenant)
            rec["status"] = "activated"
            rec["activatedAt"] = now_iso()
            rec["tenant"] = tenant_id
            _save_codes(data)
    if recovery:
        import asyncio
        r_email, r_lic, r_tid, r_old = recovery
        try:
            await asyncio.to_thread(_send_recovery_email, r_email, r_lic)
        except Exception as e:

            log.error("recovery email failed for tenant %s: %s code=%s",
                      r_tid, type(e).__name__, getattr(e, "smtp_code", None))
            return JSONResponse({"ok": False, "already": True,
                                 "error": "Письмо не ушло (SMTP). Напиши в поддержку: help@rezon-ai.ru"},
                                status_code=503, headers=_NOCACHE)
        with _codes_lock:
            data = _load_codes()
            t = next((x for x in data["tenants"] if x.get("id") == r_tid), None)
            if t:


                cur = t.get("licenseKey") or _license_for_code(data, t.get("code") or "")[0]
                if cur != r_old:
                    _add_revoked(data, r_lic, "recovery race loser")
                    _save_codes(data)
                    return JSONResponse({"ok": False, "already": True,
                                         "error": "Другое восстановление уже выполнено — ключ в почте. "
                                                  "Если письма нет, повтори через час."},
                                        status_code=409, headers=_NOCACHE)
                t["licenseKey"] = r_lic
                t["lastRecoveryAt"] = time.time()
                if r_old:
                    _add_revoked(data, r_old, f"recovery → {r_email}")

                rec2 = next((c for c in data["codes"] if c.get("code") == t.get("code")), None)
                if rec2 and rec2.get("licenseKey"):
                    rec2["licenseKey"] = r_lic
                for p in data.get("payments", []):
                    if p.get("code") == t.get("code") and p.get("licenseKey"):
                        p["licenseKey"] = r_lic
                _save_codes(data)
        _event("license_recovery", r_email,
               code if r_old else f"{code} · mint без прежней лицензии")
        return JSONResponse({"ok": True, "already": True, "sent": "email",
                             "message": "Новый ключ отправлен на email активации."}, headers=_NOCACHE)
    emailed = False
    if email and billing and os.environ.get("SMTP_HOST"):


        import asyncio
        try:
            await asyncio.to_thread(_send_redeem_email, email, lic, code)
            emailed = True
        except Exception as e:

            log.error("redeem key email failed for tenant %s: %s code=%s",
                      tenant_id, type(e).__name__, getattr(e, "smtp_code", None))
    _event("code_activated", tenant_id, f'{rec["tier"]} · {code}')
    return JSONResponse({"ok": True, "tier": rec["tier"], "token": token, "licenseKey": lic,
                         "emailed": emailed}, headers=_NOCACHE)







REFERRALS_FILE = DATA_DIR / "referrals.json"
_ref_lock = threading.Lock()

_REF_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_REF_CODE_RE = re.compile(r"^[2-9A-HJKM-NP-TV-Z]{4}-[2-9A-HJKM-NP-TV-Z]{4}$")
_REF_STRIP_RE = re.compile(r"[^2-9A-HJKM-NP-TV-Za-hjkm-np-tv-z]")


def _ref_code_for(install_id: str) -> str:


    d = hmac.new(REF_HMAC_SECRET.encode(), install_id.encode(), hashlib.sha256).digest()
    n = int.from_bytes(d[:8], "big")
    out = []
    for _ in range(8):
        n, r = divmod(n, len(_REF_ALPHABET))
        out.append(_REF_ALPHABET[r])
    raw = "".join(out)
    return f"{raw[:4]}-{raw[4:]}"


def _ref_norm(raw) -> str:

    s = _REF_STRIP_RE.sub("", str(raw or "")).upper()
    if len(s) != 8:
        return ""
    code = f"{s[:4]}-{s[4:]}"
    return code if _REF_CODE_RE.match(code) else ""


def _load_referrals():

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
        os.chmod(REFERRALS_FILE, 0o600)
    except OSError:
        pass


def _ref_lookup(raw) -> str:

    code = _ref_norm(raw)
    if not code:
        return ""
    with _ref_lock:
        refs = _load_referrals()
        if refs is None:
            log.error("referrals.json повреждён — ref-скидка пропущена")
            return ""
        return code if code in refs else ""


_REF_PENDING_TTL = 86400


_FAILED_STATUSES = ("failed", "cancelled", "canceled", "expired")


def _ref_restore_locked(refs, payment, flag):


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
    rrec["history"] = rrec["history"][-200:]
    return True


def _ref_sweep_pending():



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
    _ref_sweep_pending()
    code = _ref_code_for(iid)
    with _ref_lock:
        refs = _load_referrals()
        if refs is None:
            return JSONResponse({"ok": False, "error": "referrals store corrupted"}, status_code=500)
        rec = refs.get(code)
        if rec is None:


            rec = {"install_id": iid, "fp": known_fp, "created": now_iso(),
                   "clicks": 0, "buys": 0, "stack_n": 0, "history": []}
            refs[code] = rec
            _save_referrals(refs)
        elif rec.get("install_id") != iid:

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




    if STORE_MODE:
        return PlainTextResponse("Not Found", status_code=404)
    import asyncio
    iid = _install_id()
    fields = _client_fp_fields()

    fp = fields.get("host_fp") or fields.get("fp") or ""
    out = {"ok": False, "install_id": iid, "fp": fp}
    try:

        r = await asyncio.to_thread(_post_json, f"{PUBLIC_BASE_URL}/api/referral/code",
                                    {"install_id": iid, "fp": fp}, 8)
        if isinstance(r, dict) and r.get("ok"):
            out.update({k: r[k] for k in ("code", "link", "clicks", "buys", "stack_n") if k in r})
            out["ok"] = True
    except Exception:
        pass
    return JSONResponse(out, headers=_NOCACHE)




def _platega_create(plan_id: str, ref: str, amount: "int | None" = None) -> dict:


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

    plans = [{"id": pid, "label": PLANS[pid]["label"], "rub": PLANS[pid]["RUB"],
              "usd": PLANS[pid]["USD"], "days": PLANS[pid]["days"]}
             for pid in ("pro1", "pro3", "pro6", "pro12") if pid in PLANS]
    return JSONResponse({"plans": plans}, headers={**_NOCACHE, **_CORS})


@mcp.custom_route("/api/plans", methods=["OPTIONS"])
@mcp.custom_route("/api/billing/pay", methods=["OPTIONS"])
@mcp.custom_route("/api/billing/status", methods=["OPTIONS"])
@mcp.custom_route("/api/billing/lava/invoice", methods=["OPTIONS"])
async def billing_cors_preflight(request: Request):

    return Response(status_code=204, headers=_CORS)


@mcp.custom_route("/api/billing/pay", methods=["POST"])
async def api_billing_pay(request: Request):






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
        extend_email = _valid_email(payload.get("email")) or ""




    if body.get("email") is not None and not _valid_email(body.get("email")):
        return JSONResponse({"error": "valid email required"}, status_code=400, headers=_CORS)
    email = extend_email or (_valid_email(body.get("email")) or "")




    ref_code = "" if extend else _ref_lookup(body.get("ref"))

    buyer_iid = str(body.get("install_id") or "")
    buyer_fp = str(body.get("fp") or "")
    buyer_iid = buyer_iid if _INSTALL_ID_RE.match(buyer_iid) else ""
    buyer_fp = buyer_fp if _FP_RE.match(buyer_fp) else ""




    pct = 0
    stack_code = ""
    burned_n = 0
    if extend:
        pct = round(100 * (1 - EXTEND_DISCOUNT))
        if buyer_iid:
            with _installs_lock:
                idata = _load_installs()
                irec = (idata or {}).get("installs", {}).get(buyer_iid) if idata else None

            if irec is not None and irec.get("fp") and irec.get("fp") == buyer_fp:
                with _ref_lock:
                    refs = _load_referrals()
                    rrec = refs.get(_ref_code_for(buyer_iid)) if refs is not None else None
                    stack_n = _safe_int((rrec or {}).get("stack_n"))



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
        _ref_restore_burned(stack_code, burned_n)
        return JSONResponse({"error": "payment creation failed"}, status_code=502, headers=_CORS)
    log.info("platega create response: %s", json.dumps(resp, ensure_ascii=False)[:500])
    pdata = resp.get("data") or resp
    pay_url = pdata.get("redirect") or pdata.get("url") or pdata.get("link") or pdata.get("paymentUrl")
    platega_id = str(pdata.get("id") or pdata.get("transactionId") or "")
    if not pay_url:

        _ref_restore_burned(stack_code, burned_n)
        return JSONResponse({"error": "no pay url"}, status_code=502, headers=_CORS)
    with _codes_lock:
        data = _load_codes()
        rec = {"id": ref, "ref": ref, "platega_id": platega_id, "plan": plan_id,
               "tier": "pro", "status": "created", "at": now_iso(), "payUrl": pay_url,
               "amount": amount if amount is not None else PLANS[plan_id]["RUB"]}
        if email:
            rec["email"] = email
        if extend:
            rec["extend"] = extend
            if pct > 10:
                rec["extend_pct"] = pct
            if stack_code:
                rec["stack_code"] = stack_code
                rec["stack_ts"] = int(time.time())
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




    ip = _client_ip(request)
    if _rate_limited(ip, "paystatus", 30, 60):
        return JSONResponse({"error": "too_many_attempts"}, status_code=429, headers=_CORS)
    ref = request.query_params.get("ref", "")[:64]
    if request.method == "POST":

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
    if p.get("licenseKey"):
        out["licenseKey"] = p["licenseKey"]
    return JSONResponse(out, headers={**_NOCACHE, **_CORS})


@mcp.custom_route("/billing/result", methods=["GET"])
async def billing_result(request: Request):
    g = _store_gate()
    if g:
        return g
    return HTMLResponse(_page_html("billing.html"), headers=_NOCACHE)




@mcp.custom_route("/api/license", methods=["GET"])
async def api_license(request: Request):

    return JSONResponse(_license_status(), headers=_NOCACHE)


@mcp.custom_route("/api/version", methods=["GET"])
async def api_version(request: Request):





    return JSONResponse({"version": VERSION}, headers=_NOCACHE)


@mcp.custom_route("/api/update/trigger", methods=["POST"])
async def api_update_trigger(request: Request):




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
        pass
    return JSONResponse({"ok": True, "current": VERSION, "latest": latest}, headers=_NOCACHE)


def _release_file(suffix: str) -> Response:


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


    media = "application/octet-stream" if suffix == "sig" else "application/gzip"
    return FileResponse(p, media_type=media,
                        headers={"Cache-Control": "no-store"})


def _release_guard(request: Request) -> "Response | None":

    ip = _client_ip(request)
    if _rate_limited(ip, "releases", 30, 60):
        return JSONResponse({"ok": False, "error": "too_many_requests"}, status_code=429)
    return None


@mcp.custom_route("/api/releases/latest.tar.gz", methods=["GET"])
async def api_release_tarball(request: Request):


    guard = _release_guard(request)
    if guard is not None:
        return guard
    return _release_file("tar.gz")


@mcp.custom_route("/api/releases/latest.sha256", methods=["GET"])
async def api_release_sha(request: Request):

    guard = _release_guard(request)
    if guard is not None:
        return guard
    return _release_file("sha256")


@mcp.custom_route("/api/releases/latest.sig", methods=["GET"])
async def api_release_sig(request: Request):



    guard = _release_guard(request)
    if guard is not None:
        return guard
    return _release_file("sig")







_NATIVE_NAME_RE = re.compile(
    r"^(?:maxgraph-\d+\.\d+\.\d+-(?:windows|macos|linux)-(?:amd64|arm64)"
    r"(?:\.exe)?(?:\.sha256|\.sig)?|manifest\.json)$")


def _native_release_file(fname: str) -> Response:


    p = os.path.join(RELEASES_DIR, "native", fname)
    if fname.endswith(".sha256"):

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

        return FileResponse(p, media_type="application/octet-stream",
                            headers={"Cache-Control": "no-store"})

    return FileResponse(p, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store",
                                 "Content-Disposition": 'attachment; filename="%s"' % fname})


@mcp.custom_route("/api/releases/native/manifest", methods=["GET"])
async def api_release_native_manifest(request: Request):


    guard = _release_guard(request)
    if guard is not None:
        return guard
    return _native_release_file("manifest.json")


@mcp.custom_route("/api/releases/native/{fname}", methods=["GET"])
async def api_release_native(request: Request):


    guard = _release_guard(request)
    if guard is not None:
        return guard
    fname = request.path_params.get("fname", "")
    if not _NATIVE_NAME_RE.fullmatch(fname):
        return JSONResponse({"ok": False, "error": "no artifact"}, status_code=404)
    return _native_release_file(fname)


@mcp.custom_route("/api/license/activate", methods=["POST"])
async def api_license_activate(request: Request):

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
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    key = str(body.get("key") or "")[:512].strip()
    if not key:
        return JSONResponse({"ok": False, "error": "key required"}, status_code=400)
    try:
        import asyncio
        res = await asyncio.to_thread(_activate_key, key)
    except Exception as e:
        log.error("license activate error от %s: %s", ip, e)
        return JSONResponse({"ok": False, "error": f"activate failed: {e}"}, status_code=500)
    if not res.get("ok"):
        log.warning("license activate rejected от %s: %s", ip, res.get("error"))
        out = {"ok": False, "error": res["error"]}
        if res.get("machine_limit"):
            out["machine_limit"] = True
        return JSONResponse(out, headers=_NOCACHE)
    _event("license_activated", res.get("email") or "", res.get("tier", ""))
    log.info("license activated: %s · %s", res.get("tier"), res.get("email"))
    try:
        await asyncio.to_thread(_relay_config_pull)
    except Exception:
        pass
    return JSONResponse({"ok": True, "email": res.get("email"), "tier": res.get("tier")}, headers=_NOCACHE)


def _activate_key(key: str) -> dict:











    if not PUBLIC_BASE_URL:
        return {"ok": False, "error": "активация требует связи с сервером магазина"}
    payload = {"key": key}
    payload.update(_client_fp_fields())
    payload["install_id"] = _install_id()
    try:
        j = _post_json(f"{PUBLIC_BASE_URL}/api/license/validate", payload, timeout=6)
    except Exception as e:
        j = _http_error_json(e)
        if j is None:
            return {"ok": False, "error": f"сервер магазина недоступен — активация требует онлайн: {e}"}
    if not isinstance(j, dict):
        return {"ok": False, "error": "сервер магазина ответил не-JSON — активация отклонена"}
    if j.get("revoked"):
        return {"ok": False, "error": "license revoked"}
    if not j.get("ok"):
        if j.get("error") == "machine_limit":
            return {"ok": False, "error": j.get("message") or _MACHINE_LIMIT_MSG,
                    "machine_limit": True}
        return {"ok": False, "error": j.get("error") or "сервер магазина отклонил ключ"}
    return licensing.grant(DATA_DIR, LICENSE_SECRET, j, key=key)


def _http_error_json(e: Exception):


    import urllib.error
    if not isinstance(e, urllib.error.HTTPError):
        return None
    try:
        j = json.loads(e.read(65536).decode("utf-8"))
    except Exception:
        return None
    return j if isinstance(j, dict) else None


@mcp.custom_route("/api/license/validate", methods=["POST"])
async def api_license_validate(request: Request):










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
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    key = str(body.get("key") or "")[:512].strip()
    payload, err = licensing.verify_key(LICENSE_SECRET, key)
    if err:
        return JSONResponse({"ok": False, "error": err}, headers=_NOCACHE)
    rec = _is_revoked(_load_codes(), key)
    if rec:
        _event("license_validate_revoked", payload.get("email") or "", payload.get("tier", ""))
        return JSONResponse({"ok": False, "revoked": True, "error": "license revoked",
                             "revoked_at": rec.get("ts") or 0}, headers=_NOCACHE)
    fp = str(body.get("fp") or "")[:128].strip().lower()
    host_fp, components, fp_kind = _parse_hostfp_body(body)
    eff = host_fp or fp
    if fp and not _FP_RE.match(fp):
        return JSONResponse({"ok": False, "error": "bad fp"}, status_code=400)
    if eff and fp_kind != "container":


        try:
            with _machines_lock:
                mdata = _load_machines()
                verdict, _rec = _machine_bind(mdata, key, eff, time.time(),
                                              components=components, legacy_fp=fp,
                                              email=payload.get("email") or "")
                if verdict != "limit":
                    _save_machines(mdata)
        except Exception:
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


            _event("license_machine_rebound", payload.get("email") or "", eff[:12])
        elif verdict == "review":

            _event("license_machine_review", payload.get("email") or "", eff[:12])
    fp = eff



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
                if len(ki) > 5000:
                    data["key_installs"] = dict(sorted(ki.items(),
                                                       key=lambda kv: kv[1].get("at", ""))[-4000:])
                pay = next((p for p in data["payments"]
                            if p.get("licenseKey") == key and p.get("ref_code")), None)
                if pay is not None and not pay.get("selfref"):
                    rcode = str(pay["ref_code"])
                    with _ref_lock:
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
    if selfref_msg and TG_NOTIFY_CHAT_ID:
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



    raw = os.environ.get(name)
    v = embed._pfx(raw)
    if raw and not v:
        log.warning("%s невалиден (>32/не-printable/не-UTF-8) — дефолт %r",
                    name, default)
    return v or default



_RELAY_EMBED_QUERY_PREFIX = _relay_pfx("RELAY_EMBED_QUERY_PREFIX", "query: ")
_RELAY_EMBED_PASSAGE_PREFIX = _relay_pfx("RELAY_EMBED_PASSAGE_PREFIX", "passage: ")


@mcp.custom_route("/api/relay/config", methods=["POST"])
async def api_relay_config(request: Request):









    g = _store_gate()
    if g:
        return g
    ip = _client_ip(request)
    if _rate_limited(ip, "relayconfig", 20, 60):
        return JSONResponse({"ok": False, "error": "too_many_attempts"}, status_code=429)


    body, body_err = await _read_capped(request, 16384)
    if body_err is not None:
        return body_err
    try:
        payload = json.loads(body)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    key = str(payload.get("key") or "")[:512].strip()
    if key:
        lic, err = licensing.verify_key(LICENSE_SECRET, key)
        revoked = lic is not None and _is_revoked(_load_codes(), key)
        if err or revoked:

            log.info("relay/config отклонён от %s: %s", ip, err or "license revoked")
            _event("relay_config_rejected", "", err or "revoked")
            return JSONResponse({"ok": False, "error": "invalid key"}, status_code=403,
                                headers=_NOCACHE)
        exp = int(lic.get("exp") or 0)
        seed, tag, detail = key, lic.get("tier", ""), lic.get("email") or ""
    else:




        iid = str(payload.get("install_id") or "")
        fp = str(payload.get("fp") or "")
        host_fp, _components, _kind = _parse_hostfp_body(payload if isinstance(payload, dict) else {})
        fp = host_fp or fp
        t = None
        pair_known = False
        if _INSTALL_ID_RE.match(iid) and _FP_RE.match(fp):
            try:
                with _installs_lock:
                    data = _load_installs() or {}
                    t = (data.get("trials") or {}).get(fp)
                    pair_known = f"{iid}:{fp}" in (data.get("relay_grants") or {})
            except Exception:
                t = None
            if not isinstance(t, dict) or iid not in (t.get("installs") or []):
                t = None
                pair_known = False
        try:
            started = float((t or {}).get("started_at") or 0)
        except (TypeError, ValueError, OverflowError):
            started = 0
        exp = int(started + licensing.trial_days() * 86400) if started else 0
        if not exp or time.time() > exp:
            log.info("relay/config trial-грант отклонён от %s (unknown install или якорь истёк)", ip)
            _event("relay_config_rejected", "", "trial grant")
            return JSONResponse({"ok": False, "error": "invalid key"}, status_code=403,
                                headers=_NOCACHE)



        if not pair_known:
            if _rate_limited(ip, "relaytrial", 2, 86400):
                log.info("relay/config trial-грант: дневной кап IP %s (новая пара)", ip)
                _event("relay_config_rejected", "", "trial daily cap")
                return JSONResponse({"ok": False, "error": "too_many_attempts"},
                                    status_code=429)
            with _installs_lock:
                gdata = _load_installs()
                if gdata is not None:
                    grants = gdata.setdefault("relay_grants", {})
                    grants[f"{iid}:{fp}"] = time.time()
                    cut = time.time() - licensing.RELAY_TOKEN_TTL
                    for gk, gts in list(grants.items()):
                        if not isinstance(gts, (int, float)) or gts < cut:
                            del grants[gk]
                    try:
                        _save_installs(gdata)
                    except OSError:
                        pass
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
        "rank": _rank_config_cached()}
    return JSONResponse(out, headers=_NOCACHE)


@mcp.custom_route("/api/tenants", methods=["GET"])
async def api_tenants_list(request: Request):








    g = _store_gate()
    if g:
        return g
    if not _admin_auth(request):
        return _admin_401()
    data = _load_codes()
    try:
        mregs = _load_machines().get("machines") or {}
    except Exception:
        mregs = {}

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
        seen_kh = set()
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
            payload, _e = licensing.verify_key(LICENSE_SECRET, lk, now=0)
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
            if mrec.get("email"):
                u["email"] = mrec["email"]
            unattached.append(u)
    rows.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
    return JSONResponse({"tenants": rows[:500], "unattached": unattached[:500]},
                        headers=_NOCACHE)









_ADMIN_FILE = DATA_DIR / "admin.json"
_ADMIN_SESSIONS: dict = {}
_ADMIN_SESSION_TTL = 12 * 3600
_ADMIN_PBKDF2_ROUNDS = 200_000
_ADMIN_COOKIE_PATHS = {"/api/payments", "/api/tenants", "/api/codes", "/api/codes/generate",
                       "/api/codes/export", "/api/installs", "/api/admin/revoke",
                       "/api/admin/logout", "/api/admin/password",
                       "/api/admin/machines", "/api/admin/machines/unbind",
                       "/api/admin/synonyms", "/api/admin/synonyms/veto",
                       "/api/admin/support-reports",
                       "/api/admin/stats"}


def _admin_cfg() -> dict | None:



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
        pass


def _admin_set_password(cfg: dict, user: str, password: str) -> dict:
    salt = secrets.token_bytes(16)
    return {"user": user, "salt": salt.hex(),
            "pass_hash": hashlib.pbkdf2_hmac("sha256", password.encode(), salt,
                                             _ADMIN_PBKDF2_ROUNDS).hex()}


def _admin_check_password(user: str, password: str, cfg: dict) -> bool:


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

    auth = request.headers.get("authorization", "")
    if TOKEN and hmac.compare_digest(auth, f"Bearer {TOKEN}"):
        return True
    return STORE_MODE and _admin_session_ok(request)


def _admin_401():
    return JSONResponse({"error": "unauthorized"}, status_code=401, headers=_NOCACHE)



def _rate_peek(ip: str, bucket: str, maxn: int, window: int) -> bool:
    now = time.time()
    hits = [t for t in _rate_buckets.get(f"{bucket}:{ip}", []) if now - t < window]
    return len(hits) >= maxn


def _rate_hit(ip: str, bucket: str):
    _rate_buckets.setdefault(f"{bucket}:{ip}", []).append(time.time())


def _rate_clear(ip: str, bucket: str):
    _rate_buckets.pop(f"{bucket}:{ip}", None)


_ADMIN_SETUP_LOCK = threading.Lock()


@mcp.custom_route("/api/admin/me", methods=["GET"])
async def api_admin_me(request: Request):


    g = _store_gate()
    if g:
        return g
    cfg = _admin_cfg()
    configured = cfg is None or bool(cfg.get("pass_hash"))
    authed = _admin_auth(request)
    if not authed and configured:


        return PlainTextResponse("Not Found", status_code=404)
    return JSONResponse({"configured": configured, "authed": authed}, headers=_NOCACHE)


@mcp.custom_route("/api/admin/setup", methods=["POST"])
async def api_admin_setup(request: Request):



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
    _ADMIN_SESSIONS.clear()
    _event("admin_password_changed", cfg.get("user", ""), "")
    return JSONResponse({"ok": True}, headers={**_NOCACHE,
                                               "Set-Cookie": "mg_admin=; Path=/; HttpOnly; Max-Age=0"})


@mcp.custom_route("/api/admin/revoke", methods=["POST"])
async def api_admin_revoke(request: Request):






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
    payload, kerr = licensing.verify_key(LICENSE_SECRET, key, now=0)
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
                    if not rec["fps"]:
                        rec.pop("components", None)
                        rec.pop("review", None)
                        rec["aliases"] = []
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
        raw, _err = await _read_capped(request, 16384)
        if _err is not None:
            return JSONResponse({"error": "bad request"}, status_code=400)
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad json"}, status_code=400)
    plan_id = _plan_key(str(body.get("plan") or body.get("tier") or "")[:16])
    email = _valid_email(body.get("email")) or ""
    if not plan_id:
        return JSONResponse({"error": "plan must be pro1|pro3|pro6|pro12|donate99|donate249|donate499"}, status_code=400)
    if not email:
        return JSONResponse({"error": "valid email required"}, status_code=400)



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

        return JSONResponse({"error": "payment creation failed"}, status_code=502)
    with _codes_lock:
        data = _load_codes()

        rec = {"id": ref, "ref": ref, "platega_id": pay["payment_id"],
               "plan": plan_id, "tier": "donation" if PLANS[plan_id].get("donation") else "pro",
               "email": email, "status": "created",
               "at": now_iso(), "payUrl": pay["payment_url"]}
        if ref_code:
            rec["ref_code"] = ref_code
            rec["amount"] = amount
        data["payments"].append(rec)
        _save_codes(data)
    _event("payment_created", ref, f"{plan_id} · {email}")
    return JSONResponse({"payment_url": pay["payment_url"]}, headers=_NOCACHE)




_LAVA_PLANS = {"1": "pro1", "3": "pro3", "6": "pro6", "12": "pro12"}



_LAVA_PLAN_BY_AMOUNT = {float(PLANS[p]["USD"]): p for p in ("pro3", "pro6", "pro12")}



_LAVA_DONATE = {"d5": 5, "d10": 10, "d25": 25}


@mcp.custom_route("/api/billing/lava/invoice", methods=["POST"])
async def api_billing_lava_invoice(request: Request):







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
    if not isinstance(body, dict):
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
    except Exception as e:
        log.error("lava invoice failed: %s", e)
        return JSONResponse({"error": "payment creation failed"}, status_code=502, headers=_CORS)

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

    import html as _html
    pid = pid[:64]
    p = None
    if pid:
        data = _load_codes()


        p = next((x for x in reversed(data["payments"])
                  if pid in (x.get("id"), x.get("ref"))), None)
    paid = bool(p and p.get("status") in _PAID_STATUSES and p.get("code"))
    esc = _html.escape
    if paid and p.get("donation"):

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

    g = _store_gate()
    if g:
        return g
    return HTMLResponse(_success_page(request.query_params.get("pid", "")), headers=_NOCACHE)


@mcp.custom_route("/billing/webhook/platega", methods=["POST"])
async def billing_webhook_platega(request: Request):


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
    _ref_sweep_pending()
    raw, err = await _read_capped(request, 65536)
    if err is not None:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        body = json.loads(raw)
    except Exception:
        body = dict(await request.form()) if request.method == "POST" else {}
    if not isinstance(body, dict):
        body = {}
    delivery = None
    donation_delivery = None
    ref_msgs = []
    with _codes_lock:
        data = _load_codes()
        pid = str(body.get("id") or body.get("payment_id") or body.get("order_id") or "")[:128]
        ref = str(body.get("payload") or "")[:64]
        if not pid:
            import hashlib
            pid = "h-" + hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
        status = str(body.get("status", "")).lower()[:32]

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

                plan_id = _plan_for(amount, currency)
                _event("plan_guessed_from_amount", pid, f"{amount}{currency} → {plan_id}")
            exp_amt = payment.get("amount")
            if exp_amt and amount and abs(amount - float(exp_amt)) > 1:
                _event("payment_amount_mismatch", pid,
                       f"{plan_id} · оплачено {amount}{currency} · выставлено {exp_amt}")
            if PLANS[plan_id].get("donation"):


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

                email = (payment.get("email") or _valid_email(body.get("email"))) or None
                lic_key = licensing.generate_key(LICENSE_SECRET, email or "buyer@unknown", "pro", days)
                payment["licenseKey"] = lic_key
                if ext_ok:


                    _add_revoked(data, ext, "superseded: продление")
                if email:
                    payment["email"] = email



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
                                payment["stack_restored"] = False
                            if dirty:
                                _save_referrals(refs)
        _save_codes(data)
    if donation_delivery:
        import asyncio
        d_email, d_label = donation_delivery
        try:
            res = await asyncio.to_thread(billing.deliver_donation, label=d_label, email=d_email)
            log.info("deliver_donation: %s", res)
        except Exception as e:
            log.error("deliver_donation упал целиком (%s)", e)
    if delivery:
        import asyncio
        code, lic_key, tier, email, success_pid = delivery
        try:
            res = await asyncio.to_thread(billing.deliver_code, code=code, license_key=lic_key,
                                          tier=tier, email=email,
                                          success_url=f"{PUBLIC_BASE_URL}/billing/success?pid={success_pid}")
            log.info("deliver_code %s: %s", code, res)
        except Exception as e:
            log.error("deliver_code упал целиком (%s): код %s уже в codes.json и на success-странице", e, code)
    if ref_msgs:
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
    delivery = None
    donation_delivery = None
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


            if ev["product_id"] and ev["product_id"] != lava.PRODUCT_ID:
                log.warning("lava %s: чужой продукт %s — пропуск", etype, ev["product_id"])
                _event("lava_foreign_product", ev["event_id"], f"{etype} · {ev['product_id']}")
            else:
                hit = None
                if ev["contract_id"]:



                    hit = next((p for p in reversed(data["payments"])
                                if p.get("lava_id") == ev["contract_id"]
                                and p.get("status") != "refunded"), None)
                    if hit and hit.get("donation") and hit.get("status") != "success":
                        hit = None
                else:
                    for p in reversed(data["payments"]):

                        if not p.get("lava_id") or not (p.get("licenseKey") or p.get("donation")):
                            continue
                        if p.get("status") == "refunded":
                            continue
                        if p.get("donation") and p.get("status") != "success":
                            continue
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
                    else:
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
            data["lava_events"] = seen[-500:]
        _save_codes(data)
    if delivery:
        import asyncio
        code, lic_key, tier, email, success_pid = delivery
        try:
            res = await asyncio.to_thread(billing.deliver_code, code=code, license_key=lic_key,
                                          tier=tier, email=email, lang="en",
                                          success_url=f"{PUBLIC_BASE_URL}/billing/success?pid={success_pid}")
            log.info("lava deliver_code %s: %s", code, res)
        except Exception as e:
            log.error("deliver_code упал целиком (%s): код %s уже в codes.json и на success-странице", e, code)
    if donation_delivery:
        import asyncio
        d_email, d_label = donation_delivery
        try:
            res = await asyncio.to_thread(billing.deliver_donation, label=d_label, email=d_email)
            log.info("lava deliver_donation: %s", res)
        except Exception as e:
            log.error("deliver_donation упал целиком (%s)", e)
    return JSONResponse(result, headers=_NOCACHE)




async def _digiseller_params(request: Request) -> dict:

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







    g = _store_gate()
    if g:
        return g
    fmt = DIGISELLER_RESPONSE_FORMAT
    ip = _client_ip(request)
    if _rate_limited(ip, "digiseller_gen", 30, 60):
        return _dg_response(error="too_many_requests", fmt=fmt, status=429)

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
    delivery = None
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
    if delivery:
        import asyncio
        code, lic_key, tier, email = delivery
        try:
            dres = await asyncio.to_thread(billing.deliver_code, code=code, license_key=lic_key,
                                           tier=tier, email=email,
                                           success_url=f"{PUBLIC_BASE_URL}/billing/success?pid={urllib.parse.quote(res['payId'], safe='')}")
            log.info("digiseller deliver_code %s: %s", code, dres)
        except Exception as e:
            log.error("deliver_code упал целиком (%s): код %s уже в codes.json", e, code)



    ok_id = params.get("id") or digiseller.product_id_of(params) or str(DIGISELLER_PRODUCT_ID or "")
    ok_inv = params.get("inv") or res["order"]

    return _dg_response(code=digiseller.goods_text(res["code"]), fmt=fmt, product_id=ok_id, inv=ok_inv)




def _hook_authorized(request: Request) -> bool:
    import hmac as _h
    key = str(request.query_params.get("key") or "")[:128]
    return bool(DIGISELLER_HOOK_SECRET) and _h.compare_digest(key, DIGISELLER_HOOK_SECRET)


async def _digiseller_hook(request: Request, kind: str):

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
            os.chmod(WEBHOOKS_LOG, 0o600)
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
    if not secret:
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
        except Exception as e:
            log.error("gh hook telegram: %s", e)
    return JSONResponse({"ok": True, "delivered": bool(text)}, headers=_NOCACHE)


@mcp.custom_route("/webhook/digiseller/message", methods=["GET", "POST"])
async def digiseller_hook_message(request: Request):
    return await _digiseller_hook(request, "message")




def _tg_on_start(chat_id: str, payload: str) -> str:








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




def embed_loop(cfg: dict):




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




def _actr_flush_quiet():



    try:
        _actr.flush(ACCESS_FILE)
    except Exception:
        pass


def actr_loop(cfg: dict):




    log.info("actr: flush-цикл запущен (interval=%ss)", cfg["flush_sec"])
    while True:
        time.sleep(cfg["flush_sec"])
        try:
            _actr.flush(ACCESS_FILE)
        except Exception:
            log.exception("actr: сбой flush — продолжаем (fail-open)")




class TokenAuth:











    PUBLIC_PATHS = {"/healthz", "/", "/admin", "/app", "/landing", "/og.png", "/favicon.ico",
                    "/privacy", "/offer",
                    "/.well-known/security.txt",
                    "/api/codes/redeem", "/api/plans",
                    "/api/billing/pay", "/api/billing/status", "/billing/result", "/billing/webhook/platega",
                    "/billing/create", "/billing/success", "/api/license/validate",
                    "/api/billing/lava/invoice", "/api/webhooks/lava",
                    "/api/telemetry/install",
                    "/api/telemetry/learn",
                    "/api/telemetry/heartbeat",
                    "/api/support/report",
                    "/api/version",
                    "/api/admin/me",
                    "/api/admin/login", "/api/admin/setup",
                    "/api/relay/config",
                    "/api/referral/code",
                    "/api/releases/latest.tar.gz", "/api/releases/latest.sha256",
                    "/api/releases/latest.sig",
                    "/billing/digiseller/gen",
                    "/webhook/digiseller/sale", "/webhook/digiseller/message",
                    "/api/hooks/github"}




    PUBLIC_PREFIXES = ("/static/", "/api/releases/native/")

    if not STORE_MODE:




        PUBLIC_PATHS = PUBLIC_PATHS | {"/api/license/activate"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")




            if scope.get("method") in ("GET", "HEAD"):
                host = dict(scope.get("headers") or []).get(b"host", b"").decode("utf-8", "ignore")


                m = re.fullmatch(r"127\.0\.0\.1(?::(\d{1,5}))?", host)
                if m:

                    qs = scope.get("query_string", b"").decode("latin-1")
                    loc = ("http://localhost" + (":" + m.group(1) if m.group(1) else "")
                           + urllib.parse.quote(path, safe="/%") + (f"?{qs}" if qs else ""))
                    resp = Response(status_code=308, headers={"location": loc})
                    await resp(scope, receive, send)
                    return
            if path not in self.PUBLIC_PATHS and not path.startswith(self.PUBLIC_PREFIXES):
                headers = dict(scope.get("headers") or [])
                auth = headers.get(b"authorization", b"").decode("utf-8", "ignore")
                ok = bool(TOKEN) and hmac.compare_digest(auth, f"Bearer {TOKEN}")
                if not ok and STORE_MODE and path in _ADMIN_COOKIE_PATHS:


                    ok = _admin_session_ok_cookie(
                        headers.get(b"cookie", b"").decode("utf-8", "ignore"))
                if not ok:
                    resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                    await resp(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def main():
    _require_store_token()
    _migrate_to_shards()
    st = _license_status()
    log.info("license: %s (days_left=%s)%s", st.get("state"), st.get("days_left"),
             "" if licensing.allows_write(st) else " — МУТАЦИИ ЗАБЛОКИРОВАНЫ, activate: /#activate")
    if not STORE_MODE and st.get("fp_class") == "unanchored":



        log.warning("установка не привязана к железу хоста (нет hostfp.json/HOST_FP) — "
                    "учёт триала/машин будет неточным; лечение: пере-run install.sh/install.ps1 "
                    "(досчитает hostfp.json), затем recreate контейнера")


    if _TOKEN_SOURCE == "generated":
        log.info("MEMORY_TOKEN сгенерирован и сохранён в data/local_token.txt: %s", TOKEN)
    elif _TOKEN_SOURCE == "ephemeral":
        log.warning("MEMORY_TOKEN: data/ read-only — эфемерный токен процесса: %s", TOKEN)
    if not STORE_MODE:
        threading.Thread(target=_bootstrap_hosted, daemon=True, name="bootstrap-hosted").start()
        threading.Thread(target=_license_watchdog, daemon=True, name="license-watchdog").start()
        learn.start_learn_loop(post=_post_json, base_url=PUBLIC_BASE_URL)



        import native
        native.start_heartbeat(_post_json, PUBLIC_BASE_URL, DATA_DIR, lambda: VERSION)
    rankcfg.load(RANK_CONFIG_FILE)
    env_key = os.environ.get("LICENSE_KEY", "").strip()
    if env_key and not STORE_MODE and st.get("state") != "licensed":
        res = _activate_key(env_key)
        log.info("LICENSE_KEY: %s", f"ok ({res.get('tier')} · {res.get('email')})" if res.get("ok")
                                   else f"отклонён: {res.get('error')}")
    migrate_token_hashes()
    app = TokenAuth(mcp.streamable_http_app())
    if os.environ.get("TG_BOT_TOKEN"):
        try:
            import tgbot
            tgbot.start_poller(os.environ["TG_BOT_TOKEN"], _tg_on_start)
            log.info("tgbot: deep-link доставка активна (/start <ref>)")
        except ImportError:
            log.warning("tgbot: TG_BOT_TOKEN задан, но компонент отсутствует в этой сборке")
    global EMBED_ON
    emb_cfg = embed.config_from_env()
    EMBED_ON = embed.should_start(emb_cfg, READONLY)
    if EMBED_ON:
        threading.Thread(target=embed_loop, args=(emb_cfg,), daemon=True,
                         name="embed").start()
        log.info("embed: enabled (model=%s, hybrid_w=%s)", emb_cfg["model"],
                 emb_cfg["hybrid_weight"])
    else:
        log.info("embed: disabled (%s)",
                 "READONLY mirror" if embed.enabled(emb_cfg) else "no EMBED_URL/KEY")




    actr_cfg = actr.config_from_env()
    if not READONLY:
        threading.Thread(target=actr_loop, args=(actr_cfg,), daemon=True,
                         name="actr").start()
        atexit.register(_actr_flush_quiet)
    log.info("actr: boost %s (max_boost=%s, half_life=%sd, flush=%ss%s)",
             "on" if actr_cfg["max_boost"] > 0 else "off (ACTR_MAX_BOOST=0)",
             actr_cfg["max_boost"], actr_cfg["half_life_days"], actr_cfg["flush_sec"],
             "" if not READONLY else ", flush off (READONLY mirror)")


    _coll = _canon_collisions(_load()[0])
    if _coll:
        log.warning("project-slug: коллизии транслита (разные написания слились "
                    "в один проект): %s", _coll)
    log.info("maxgraph %s listening on %s:%s (store: %s, store_mode=%s, trust_proxy=%s, readonly=%s)",
             VERSION, HOST, PORT, MEMORY_FILE, STORE_MODE, TRUST_PROXY, READONLY)
    import uvicorn


    uvicorn.run(app, host=HOST, port=PORT, log_level="info", proxy_headers=True,
                **({"log_config": None} if _FROZEN else {}))


if __name__ == "__main__":




    if _FROZEN and sys.argv[1:2] == ["--mg-apply-update"]:
        import selfupdate
        sys.exit(selfupdate.apply_update_main(sys.argv[2:]))

    if _FROZEN:
        import native
        import singleinstance
        if not singleinstance.acquire(DATA_DIR):




            log.info("single-instance: экземпляр уже запущен (data=%s) — дашборд живого, выход", DATA_DIR)
            native.open_dashboard(PORT)
            sys.exit(0)



        TOKEN = _resolve_token()
        native.ensure_hostfp()
        native.ensure_autostart()
        native.ensure_shortcuts()


        native.ensure_hook_files(token=TOKEN, port=PORT)
        import selfupdate
        selfupdate.start_update_loop(lambda: VERSION, PUBLIC_BASE_URL, DATA_DIR, PORT,
                                     on_update=native.notify_update_available)
        if os.environ.get("MG_NO_TRAY") != "1":
            native.run_tray(main)
        else:
            main()
    else:



        if not STORE_MODE and not os.path.exists("/.dockerenv"):
            import singleinstance
            if not singleinstance.acquire(DATA_DIR):
                log.info("single-instance: экземпляр уже запущен (data=%s) — выход", DATA_DIR)
                sys.exit(0)
            TOKEN = _resolve_token()
            import native
            native.ensure_hostfp()
        main()
