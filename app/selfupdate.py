"""Самообновление нативного exe-клиента по подписанному каналу магазина (T4).

Зеркало services/updater/updater.sh, но для onefile-exe без Docker:
  1) check(): GET {store}/api/version → {"version": "X.Y.Z"} (fail-open);
  2) download_and_verify(): GET {store}/api/releases/native/{name}[.sha256|.sig]
     (кап 256 МБ, стриминг в файл; 404 → магазин ещё не раздаёт native → None);
     сверка sha256 (формат sha256sum: "<hex>  <name>") + ed25519-подпись над
     САМИМ артефактом (как в deploy-скрипте: pkeyutl -sign -rawin -in tarball),
     trust anchor — release.pub.pem рядом с модулем (в exe — sys._MEIPASS);
  3) apply_update(): Windows не даёт заменить бегущий exe → helper-паттерн:
     exe перезапускает СЕБЯ с `--mg-apply-update <new> <parent_pid> <sha256> <sig>`
     (detached), родитель умирает, helper дожидается смерти parent_pid,
     ПЕРЕПРОВЕРЯЕТ sha256+ed25519 тех байтов, которые ставит (B-1: локальный
     вызов helper'а с неподписанным файлом → отказ; закрывает и TOCTOU
     «verify в update_loop, move до 120с спустя»), затем атомарно:
     текущий → maxgraph.prev.ext, new → текущий, запуск, healthcheck 30с
     (GET http://localhost:{PORT}/healthz), провал → откат prev → текущий.
     prev.exe НЕ удаляется — это и есть механизм отката; живёт до следующего
     успешного обновления (MINOR-6, решение: не чинить).

Только stdlib. Все сетевые/ФС ошибки — log.warning, цикл живёт (fail-open
на скачивании, fail-CLOSED на криптографии: битая сумма/подпись → None).
MG_NO_UPDATE=1 — update-цикл не стартует. MG_UPDATE_INTERVAL_SEC — интервал
(для тестов; дефолт 3600).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import platform as _platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("maxgraph.selfupdate")

MAX_ARTIFACT_BYTES = 256 * 1024 * 1024  # как у updater.sh (pkeyutl читает в RAM)
_CHUNK = 1 << 16
_UPDATE_FLAG = "--mg-apply-update"

# ---------------------------------------------------------------------------
# Ed25519 verify (RFC 8032), компактная чистая реализация на stdlib.
# Только verify: клиенту подписывать нечего, секретный ключ живёт на VPS.
# ---------------------------------------------------------------------------

_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = x * _I % _Q
    if x & 1:
        x = _Q - x
    return x


_BY = 4 * pow(5, _Q - 2, _Q) % _Q
_B = (_xrecover(_BY), _BY)
_IDENTITY = (0, 1)


def _edwards_add(P, Q):
    x1, y1 = P
    x2, y2 = Q
    dd = _D * x1 * x2 * y1 * y2 % _Q
    x3 = (x1 * y2 + x2 * y1) * pow((1 + dd) % _Q, _Q - 2, _Q) % _Q
    y3 = (y1 * y2 + x1 * x2) * pow((1 - dd) % _Q, _Q - 2, _Q) % _Q
    return x3, y3


def _scalarmult(P, e: int):
    Q = _IDENTITY
    while e > 0:
        if e & 1:
            Q = _edwards_add(Q, P)
        P = _edwards_add(P, P)
        e >>= 1
    return Q


def _decode_point(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    if y >= _Q:
        return None
    x = _xrecover(y)
    if (-x * x + y * y - 1 - _D * x * x * y * y) % _Q != 0:
        return None  # точка не на кривой
    if (x & 1) != (s[31] >> 7):
        x = _Q - x
    return (x, y)


def _prime_subgroup(P) -> bool:
    """P лежит в prime-order подгруппе (порядок L) и не identity.

    Без этого identity/small-order A и R пропускали подпись A=identity,
    R=identity, s=0 (тождество [s]B == R + [h]A выполняется при любом h) —
    MINOR-1 security review 2026-10-18. [L]P == identity для всех точек
    подгруппы порядка L; у точек порядка 2/4/8 (L нечётно) [L]P != identity.
    """
    if P == _IDENTITY:
        return False
    return _scalarmult(P, _L) == _IDENTITY


def ed25519_verify(pubkey32: bytes, message: bytes, sig64: bytes) -> bool:
    """True, если sig64 — валидная ed25519-подпись message ключом pubkey32."""
    if len(pubkey32) != 32 or len(sig64) != 64:
        return False
    A = _decode_point(pubkey32)
    R = _decode_point(sig64[:32])
    if A is None or R is None:
        return False
    if not _prime_subgroup(A) or not _prime_subgroup(R):
        return False  # small-order/identity pubkey или R (MINOR-1)
    s = int.from_bytes(sig64[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(sig64[:32] + pubkey32 + message).digest(), "little") % _L
    return _scalarmult(_B, s) == _edwards_add(R, _scalarmult(A, h))


_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")  # DER-обёртка ed25519 SPKI


def parse_pem_ed25519_pubkey(pem_text) -> bytes:
    """PEM SubjectPublicKeyInfo → raw 32-байтовый публичный ключ ed25519."""
    if isinstance(pem_text, bytes):
        pem_text = pem_text.decode("ascii")
    body = "".join(l.strip() for l in pem_text.strip().splitlines()
                   if l.strip() and "-----" not in l)
    der = base64.b64decode(body)
    if len(der) != 44 or not der.startswith(_SPKI_PREFIX):
        raise ValueError("не ed25519 SubjectPublicKeyInfo (DER 44 байта)")
    return der[-32:]


def _resource_base() -> Path:
    # exe: ресурсы распакованы в sys._MEIPASS; исходники — рядом с модулем
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _load_embedded_pubkey() -> bytes:
    """Вшитый trust anchor (services/updater/release.pub.pem → ресурс сборки)."""
    return parse_pem_ed25519_pubkey((_resource_base() / "release.pub.pem").read_bytes())


# ---------------------------------------------------------------------------
# Версии и имена артефактов
# ---------------------------------------------------------------------------

def _parse_semver(v):
    parts = str(v).strip().split(".")
    if not 1 <= len(parts) <= 3 or any(not p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def semver_newer(latest, current) -> bool:
    """latest строго новее current (X.Y.Z по числам; X.Y → X.Y.0; мусор → False)."""
    a, b = _parse_semver(latest), _parse_semver(current)
    return a is not None and b is not None and a > b


def artifact_name(version, platform=None, arch=None):
    """maxgraph-{v}-windows-amd64.exe / -macos-arm64 / -linux-amd64; None на неизвестной ОС."""
    plat = (platform or sys.platform).lower()
    arch = (arch or _platform.machine()).lower()
    osname = {"win32": "windows", "darwin": "macos"}.get(plat)
    if osname is None and plat.startswith("linux"):
        osname = "linux"
    archname = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(arch)
    if not osname or not archname:
        return None
    name = f"maxgraph-{version}-{osname}-{archname}"
    return name + ".exe" if osname == "windows" else name


# ---------------------------------------------------------------------------
# Сеть: check + download_and_verify
# ---------------------------------------------------------------------------

def check(store_url, current) -> dict:
    """GET {store}/api/version → {"version": ..., "newer": bool}; сбой → {} (fail-open)."""
    try:
        req = urllib.request.Request(store_url.rstrip("/") + "/api/version",
                                     headers={"User-Agent": "maxgraph-selfupdate"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read(65536).decode("utf-8", "replace"))
    except Exception as e:
        log.warning("selfupdate: check не удался: %r", e)
        return {}
    latest = str(data.get("version") or "").strip() if isinstance(data, dict) else ""
    if not latest:
        return {}
    return {"version": latest, "newer": semver_newer(latest, current)}


class _NotFound(Exception):
    pass


class _TooBig(Exception):
    pass


def _fetch_bytes(url: str, cap: int, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "maxgraph-selfupdate"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(cap + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _NotFound(url) from e
        raise
    if len(data) > cap:
        raise _TooBig(url)
    return data


def _fetch_to_file(url: str, dest: Path, cap: int, timeout: int = 300):
    """Стриминг в dest; возвращает sha256-hex. 404 → _NotFound, переполнение → _TooBig."""
    req = urllib.request.Request(url, headers={"User-Agent": "maxgraph-selfupdate"})
    h = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    raise _TooBig(url)
                h.update(chunk)
                f.write(chunk)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _NotFound(url) from e
        raise
    return h.hexdigest()


def _parse_sha256_file(text: str):
    """Формат sha256sum: "<hex>  <name>" (или "<hex> *<name>"). → (hex, name)."""
    fields = text.strip().split()
    if len(fields) < 2:
        raise ValueError("битый .sha256")
    hexdigest, name = fields[0].lower(), fields[1].lstrip("*")
    if len(hexdigest) != 64 or any(c not in "0123456789abcdef" for c in hexdigest):
        raise ValueError("битый hex в .sha256")
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError("подозрительное имя в .sha256")
    return hexdigest, name


def download_and_verify(store_url, version, dest_dir, pubkey_pem=None):
    """Скачать native-артефакт версии и проверить sha256 + ed25519-подпись.

    → Path на проверенный файл; None — магазин не раздаёт native (404,
    fail-open) или ЛЮБАЯ проверка не прошла (fail-closed на криптографии).
    """
    name = artifact_name(version)
    if not name:
        log.warning("selfupdate: неизвестная платформа %s/%s", sys.platform, _platform.machine())
        return None
    base = store_url.rstrip("/") + "/api/releases/native/"
    dest = Path(dest_dir)
    tmp = sha_tmp = sig_tmp = None
    try:
        dest.mkdir(parents=True, exist_ok=True)
        tmp = dest / (name + ".part")
        digest = _fetch_to_file(base + name, tmp, MAX_ARTIFACT_BYTES)

        sha_tmp = dest / (name + ".sha256.part")
        sha_tmp.write_bytes(_fetch_bytes(base + name + ".sha256", 4096))
        want_hex, _sum_name = _parse_sha256_file(sha_tmp.read_text(encoding="ascii", errors="replace"))
        if digest != want_hex:
            log.warning("selfupdate: sha256 НЕ СОШЛАСЬ для %s — отклонено", name)
            return None

        sig_tmp = dest / (name + ".sig.part")
        sig = _fetch_bytes(base + name + ".sig", 1024)
        sig_tmp.write_bytes(sig)

        pub = parse_pem_ed25519_pubkey(pubkey_pem) if pubkey_pem else _load_embedded_pubkey()
        if not ed25519_verify(pub, tmp.read_bytes(), sig):
            log.warning("selfupdate: ПОДПИСЬ НЕВАЛИДНА для %s — отклонено", name)
            return None

        final = dest / name
        os.replace(tmp, final)
        tmp = None
        # B-1: sidecar'ы (.sha256/.sig) ДОЖИВАЮТ до helper'а — он перепроверяет
        # те байты, которые реально ставит. Чистит их helper после успешной
        # подмены; при отказе/неудаче update-цикла download их перезапишет.
        os.replace(sha_tmp, dest / (name + ".sha256"))
        sha_tmp = None
        os.replace(sig_tmp, dest / (name + ".sig"))
        sig_tmp = None
        log.info("selfupdate: %s скачан и проверен (%s)", name, digest[:16])
        return final
    except _NotFound:
        log.info("selfupdate: %s нет на магазине (404) — native-канал ещё не раздаёт", name)
        return None
    except Exception as e:
        log.warning("selfupdate: скачивание/проверка %s: %r", name, e)
        return None
    finally:
        for p in (tmp, sha_tmp, sig_tmp):
            try:
                if p is not None and p.exists():
                    p.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Применение: helper-процесс (Windows не даёт заменить бегущий exe)
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=10).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_pid_exit(pid: int, timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.5)
    return not _pid_alive(pid)


def _spawn_detached(argv):
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True)
    if sys.platform == "win32":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def apply_update(new_exe_path) -> bool:
    """Из РАБОТАЮЩЕГО приложения: породить helper и вернуть True (вызывающий
    после этого обязан завершить процесс — helper дождётся и подменит exe).
    B-1: helper'у передаются sidecar'ы (.sha256/.sig, оставленные
    download_and_verify) — без них helper откажет (fail-closed)."""
    try:
        exe = str(Path(sys.executable).resolve())
        new = str(new_exe_path)
        argv = [exe, _UPDATE_FLAG, new, str(os.getpid())]
        sha, sig = new + ".sha256", new + ".sig"
        if os.path.exists(sha) and os.path.exists(sig):
            argv += [sha, sig]
        _spawn_detached(argv)
        log.info("selfupdate: helper запущен (%s → %s)", new_exe_path, exe)
        return True
    except Exception as e:
        log.warning("selfupdate: helper не запустился: %r", e)
        return False


def _healthz(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/healthz", timeout=timeout) as r:
            return r.read(4096).strip() == b"ok"  # строгое равенство (MINOR-5)
    except Exception:
        return False


def _helper_data_dir() -> Path:
    """DATA_DIR как в app.py (_default_data_dir): MEMORY_DIR, иначе ./data
    рядом с exe. Helper наследует env и cwd родителя → относительный
    MEMORY_DIR резолвится так же, как у родителя."""
    env_dir = os.environ.get("MEMORY_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    return Path(sys.executable).resolve().parent / "data"


def _reverify_downloaded(new_path: Path, sha_file: Path, sig_file: Path) -> bool:
    """B-1: перепроверка РОВНО тех байтов, которые helper ставит: sha256 файла
    против .sha256 и ed25519-подпись файла против .sig вшитым ключом."""
    try:
        want_hex, _ = _parse_sha256_file(
            sha_file.read_text(encoding="ascii", errors="replace"))
        data = new_path.read_bytes()
        if len(data) > MAX_ARTIFACT_BYTES:
            return False
        if hashlib.sha256(data).hexdigest() != want_hex:
            return False
        return ed25519_verify(_load_embedded_pubkey(), data, sig_file.read_bytes())
    except Exception as e:
        log.warning("selfupdate: helper: перепроверка sidecar'ов: %r", e)
        return False


def apply_update_main(argv) -> int:
    """Точка входа helper-процесса:
    argv = [<new_exe>, <parent_pid>, <sha256_file>, <sig_file>].
    B-1: fail-closed — мусорный pid, путь вне DATA_DIR/updates, отсутствующие
    sidecar'ы или битая sha256/подпись → отказ (2), ничего не трогаем."""
    if len(argv) < 4:
        log.error("selfupdate: helper: нужны <new_exe> <parent_pid> <sha256_file> <sig_file>")
        return 2
    new_arg, pid_arg, sha_arg, sig_arg = argv[0], argv[1], argv[2], argv[3]
    # (а) pid обязан быть положительным числом — никакой «подмены сразу»
    try:
        parent_pid = int(pid_arg)
    except (TypeError, ValueError):
        parent_pid = -1
    if parent_pid <= 0:
        log.error("selfupdate: helper: невалидный parent_pid %r — отказ", pid_arg)
        return 2
    # (б) new обязан лежать внутри DATA_DIR/updates — чужие пути отклоняем
    try:
        allowed = (_helper_data_dir() / "updates").resolve()
        new_path = Path(new_arg).resolve()
        new_path.relative_to(allowed)  # ValueError → вне updates
    except (ValueError, OSError) as e:
        log.error("selfupdate: helper: %s вне DATA_DIR/updates — отказ (%r)", new_arg, e)
        return 2
    exe = Path(sys.executable).resolve()
    # prev — механизм отката (MINOR-6: НЕ удалять; живёт до следующего
    # успешного обновления, затирается при нём).
    prev = exe.with_name(exe.stem + ".prev" + exe.suffix)
    try:
        port = int(os.environ.get("MG_PORT") or os.environ.get("PORT") or "8787")
    except ValueError:
        port = 8787

    if not _wait_pid_exit(parent_pid):
        log.warning("selfupdate: parent %s не умер за 120с — отмена подмены", parent_pid)
        return 1
    # (в) перепроверка ПОСЛЕ ожидания, ПЕРЕД подменой — закрывает TOCTOU:
    # между верификацией в update_loop и move могло пройти до 120с.
    if not _reverify_downloaded(new_path, Path(sha_arg), Path(sig_arg)):
        log.error("selfupdate: helper: sha256/подпись %s не сошлись — отказ, ничего не трогаем",
                  new_path)
        return 2
    try:
        os.replace(exe, prev)          # rename бегущего exe на Windows разрешён
        shutil.move(str(new_path), exe)
    except OSError as e:
        log.error("selfupdate: подмена не удалась: %r", e)
        try:
            if not exe.exists() and prev.exists():
                os.replace(prev, exe)  # не оставляем машину без exe
        except OSError:
            pass
        return 1
    for p in (sha_arg, sig_arg):       # sidecar'ы отработали — чистим
        try:
            os.unlink(p)
        except OSError:
            pass

    proc = _spawn_detached([str(exe)])
    deadline = time.monotonic() + 30
    ok = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break  # новый exe упал сразу
        if _healthz(port):
            ok = True
            break
        time.sleep(1)
    if ok:
        log.info("selfupdate: обновление применено, healthz зелёный")
        return 0

    log.warning("selfupdate: новая версия не поднялась — откат на prev")
    try:
        proc.kill()
    except Exception:
        pass
    try:
        exe.unlink(missing_ok=True)
        os.replace(prev, exe)
        _spawn_detached([str(exe)])
        log.info("selfupdate: откат выполнен")
    except OSError as e:
        log.error("selfupdate: ОТКАТ НЕ УДАЛСЯ: %r (восстанови %s → %s вручную)", e, prev, exe)
    return 1


# ---------------------------------------------------------------------------
# Фоновый цикл
# ---------------------------------------------------------------------------

def update_loop(get_version, store_url, data_dir, port, interval=None) -> None:
    """Daemon-цикл: раз в interval (env MG_UPDATE_INTERVAL_SEC, дефолт 3600)
    + по маркеру data_dir/.update-now: check → новее → download_and_verify →
    apply_update + os._exit(0) (helper подменит exe и перезапустит)."""
    if interval is None:
        try:
            interval = int(os.environ.get("MG_UPDATE_INTERVAL_SEC") or 3600)
        except ValueError:
            interval = 3600
    interval = max(1, interval)
    marker = Path(data_dir) / ".update-now"
    log.info("selfupdate: цикл запущен (interval=%ss, store=%s)", interval, store_url)
    while True:
        due = time.monotonic() + interval
        while time.monotonic() < due:
            try:
                if marker.exists():
                    marker.unlink()  # канал без данных: версию берём с магазина
                    log.info("selfupdate: триггер .update-now — проверяю сейчас")
                    break
            except OSError as e:
                log.warning("selfupdate: маркер: %r", e)
            time.sleep(min(5, max(1, due - time.monotonic())))
        try:
            info = check(store_url, get_version())
            latest = info.get("version", "")
            if not (latest and info.get("newer")):
                continue
            log.info("selfupdate: доступна %s (текущая %s) — скачиваю", latest, get_version())
            path = download_and_verify(store_url, latest, Path(data_dir) / "updates")
            if path and apply_update(path):
                time.sleep(1)   # дать helper'у подняться до смерти родителя
                os._exit(0)     # helper ждёт parent_pid, подменит и перезапустит
        except Exception as e:
            log.warning("selfupdate: итерация цикла: %r", e)


def _store_url_allowed(store_url) -> bool:
    """https — всегда; http — ТОЛЬКО loopback netloc (localhost/127.0.0.1,
    с портом или без). Подстрока «localhost» пропускала
    http://localhost.evil.tld (MINOR-2)."""
    try:
        u = urllib.parse.urlparse(str(store_url or "").strip())
        host = (u.hostname or "").lower()
    except ValueError:
        return False
    if u.scheme == "https" and host:
        return True
    return u.scheme == "http" and host in ("localhost", "127.0.0.1")


def start_update_loop(get_version, store_url, data_dir, port):
    """Запустить daemon-поток обновлений. MG_NO_UPDATE=1 → не стартует (None).
    Открытый (не-https) канал до не-локального магазина → не стартует:
    подпись по открытому каналу бессмысленна (зеркало гарда updater.sh)."""
    if os.environ.get("MG_NO_UPDATE") == "1":
        log.info("selfupdate: MG_NO_UPDATE=1 — автообновление выключено")
        return None
    if not _store_url_allowed(store_url):
        log.warning("selfupdate: store_url не https и не loopback — автообновление отключено")
        return None
    t = threading.Thread(target=update_loop, args=(get_version, store_url, data_dir, port),
                         daemon=True, name="selfupdate")
    t.start()
    return t
