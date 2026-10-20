












































from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import platform as _platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("maxgraph.selfupdate")

MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
_CHUNK = 1 << 16
_UPDATE_FLAG = "--mg-apply-update"
FALLBACK_AFTER_SEC = 7 * 24 * 3600
UPDATE_STATE_NAME = "update-state.json"



UPDATE_PENDING = "pending"






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
        return None
    if (x & 1) != (s[31] >> 7):
        x = _Q - x
    return (x, y)


def _prime_subgroup(P) -> bool:







    if P == _IDENTITY:
        return False
    return _scalarmult(P, _L) == _IDENTITY


def ed25519_verify(pubkey32: bytes, message: bytes, sig64: bytes) -> bool:

    if len(pubkey32) != 32 or len(sig64) != 64:
        return False
    A = _decode_point(pubkey32)
    R = _decode_point(sig64[:32])
    if A is None or R is None:
        return False
    if not _prime_subgroup(A) or not _prime_subgroup(R):
        return False
    s = int.from_bytes(sig64[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(sig64[:32] + pubkey32 + message).digest(), "little") % _L
    return _scalarmult(_B, s) == _edwards_add(R, _scalarmult(A, h))


_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def parse_pem_ed25519_pubkey(pem_text) -> bytes:

    if isinstance(pem_text, bytes):
        pem_text = pem_text.decode("ascii")
    body = "".join(l.strip() for l in pem_text.strip().splitlines()
                   if l.strip() and "-----" not in l)
    der = base64.b64decode(body)
    if len(der) != 44 or not der.startswith(_SPKI_PREFIX):
        raise ValueError("не ed25519 SubjectPublicKeyInfo (DER 44 байта)")
    return der[-32:]


def _resource_base() -> Path:

    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _load_embedded_pubkey() -> bytes:

    return parse_pem_ed25519_pubkey((_resource_base() / "release.pub.pem").read_bytes())






def _parse_semver(v):
    parts = str(v).strip().split(".")
    if not 1 <= len(parts) <= 3 or any(not p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def semver_newer(latest, current) -> bool:

    a, b = _parse_semver(latest), _parse_semver(current)
    return a is not None and b is not None and a > b


def artifact_name(version, platform=None, arch=None):

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






def fetch_native_manifest(store_url) -> dict:

    try:
        req = urllib.request.Request(store_url.rstrip("/") + "/api/releases/native/manifest",
                                     headers={"User-Agent": "maxgraph-selfupdate"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read(65536).decode("utf-8", "replace"))
    except Exception as e:
        log.warning("selfupdate: манифест native-раздачи не получен: %r", e)
        return {}
    return data if isinstance(data, dict) else {}


def _manifest_has_artifact(manifest: dict, version, platform=None, arch=None) -> bool:

    name = artifact_name(version, platform, arch)
    if not name:
        return False
    arts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(arts, list):
        return False
    return any(isinstance(a, dict) and a.get("name") == name for a in arts)


def native_ready(store_url, version, platform=None, arch=None) -> bool:










    name = artifact_name(version, platform, arch)
    if not name:
        return False
    m = fetch_native_manifest(store_url)
    if not m:
        return True
    return _manifest_has_artifact(m, version, platform, arch)


def check(store_url, current) -> dict:





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
    if not semver_newer(latest, current):
        return {"version": latest, "newer": False}
    if native_ready(store_url, latest):
        return {"version": latest, "newer": True}
    log.info("selfupdate: магазин объявляет %s, но native-артефакт ещё не "
             "раздаётся — ждём следующего цикла", latest)
    return {"version": latest, "newer": False, "native_pending": True}


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
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def apply_update(new_exe_path) -> bool:




    try:
        exe = str(Path(sys.executable).resolve())
        new = str(new_exe_path)




        helper_exe = str(Path(new).with_name(".helper.exe"))
        shutil.copy2(new, helper_exe)
        argv = [helper_exe, _UPDATE_FLAG, new, str(os.getpid())]
        sha, sig = new + ".sha256", new + ".sig"
        if os.path.exists(sha) and os.path.exists(sig):
            argv += [sha, sig]
        argv.append(exe)
        _spawn_detached(argv)
        log.info("selfupdate: helper запущен (%s → %s)", new_exe_path, exe)
        return True
    except Exception as e:
        log.warning("selfupdate: helper не запустился: %r", e)
        return False


def _healthz(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/healthz", timeout=timeout) as r:
            return r.read(4096).strip() == b"ok"
    except Exception:
        return False


def _start_detached(exe: Path):


    if sys.platform == "win32":
        os.startfile(str(exe))  # noqa: S606 — путь наш, подпись проверена выше
        return None
    return _spawn_detached([str(exe)])


def _refresh_icon_cache() -> None:











    if sys.platform != "win32":
        return




    ie4uinit = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                            "System32", "ie4uinit.exe")
    try:
        if not os.path.isfile(ie4uinit):
            log.warning("selfupdate: %s не найден — иконки обновятся сами позже", ie4uinit)
            return
        subprocess.Popen([ie4uinit, "-show"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log.info("selfupdate: ie4uinit -show запущен — кэш иконок обновится")
    except Exception as e:
        log.warning("selfupdate: ie4uinit -show не запустился (иконки обновятся "
                    "сами позже): %r", e)


def _retry_fileop(fn, attempts: int = 30, delay: float = 1.0):




    for i in range(attempts):
        try:
            return fn()
        except OSError as e:
            if i >= attempts - 1:
                raise
            log.warning("selfupdate: файловая операция не удалась (%r), "
                        "попытка %s/%s — повтор через %ss", e, i + 1, attempts, delay)
            time.sleep(delay)


def _wait_port_free(port: int, timeout: float = 30.0) -> bool:




    deadline = time.monotonic() + timeout
    while True:
        busy = False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                busy = True
        except OSError:
            pass
        if not busy:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                busy = True
            finally:
                s.close()
        if not busy:
            return True
        if time.monotonic() >= deadline:
            log.warning("selfupdate: порт %s занят более %ss — запускаю всё равно",
                        port, timeout)
            return False
        time.sleep(0.5)


def _helper_data_dir() -> Path:



    env_dir = os.environ.get("MEMORY_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    return Path(sys.executable).resolve().parent / "data"


def _setup_helper_log(target_exe=None) -> None:



    if not getattr(sys, "frozen", False):
        return
    try:
        base = Path(target_exe).resolve().parent / "data" if target_exe else _helper_data_dir()
        fh = logging.FileHandler(base / "update.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
    except OSError:
        pass


def _reverify_downloaded(new_path: Path, sha_file: Path, sig_file: Path) -> bool:


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
        log.warning("selfupdate: helper: перепроверка sidecar'ов: %r (файл: %s)",
                    e, getattr(e, "filename", "?"))
        return False


def apply_update_main(argv) -> int:




    if len(argv) < 4:
        log.error("selfupdate: helper: нужны <new_exe> <parent_pid> <sha256_file> <sig_file>")
        return 2
    new_arg, pid_arg, sha_arg, sig_arg = argv[0], argv[1], argv[2], argv[3]


    target_exe = argv[4] if len(argv) > 4 else None
    _setup_helper_log(target_exe)

    try:
        parent_pid = int(pid_arg)
    except (TypeError, ValueError):
        parent_pid = -1
    if parent_pid <= 0:
        log.error("selfupdate: helper: невалидный parent_pid %r — отказ", pid_arg)
        return 2


    target_dir = (Path(target_exe).resolve().parent / "data") if target_exe else _helper_data_dir()
    try:
        allowed = (target_dir / "updates").resolve()
        new_path = Path(new_arg).resolve()
        new_path.relative_to(allowed)
    except (ValueError, OSError) as e:
        log.error("selfupdate: helper: %s вне DATA_DIR/updates — отказ (%r)", new_arg, e)
        return 2
    exe = Path(target_exe).resolve() if target_exe else Path(sys.executable).resolve()


    prev = exe.with_name(exe.stem + ".prev" + exe.suffix)
    try:
        port = int(os.environ.get("MG_PORT") or os.environ.get("PORT") or "8787")
    except ValueError:
        port = 8787

    if not _wait_pid_exit(parent_pid):
        log.warning("selfupdate: parent %s не умер за 120с — отмена подмены", parent_pid)
        return 1


    if not _reverify_downloaded(new_path, Path(sha_arg), Path(sig_arg)):
        log.error("selfupdate: helper: sha256/подпись %s не сошлись — отказ, ничего не трогаем",
                  new_path)
        _start_detached(exe)
        return 2
    try:


        _retry_fileop(lambda: os.replace(exe, prev))
        _retry_fileop(lambda: shutil.move(str(new_path), exe))
    except OSError as e:
        log.error("selfupdate: подмена не удалась после ретраев: %r", e)
        try:
            if not exe.exists() and prev.exists():
                _retry_fileop(lambda: os.replace(prev, exe))
        except OSError as e2:
            log.error("selfupdate: ОТКАТ подмены не удался: %r "
                      "(восстанови %s → %s вручную)", e2, prev, exe)
        if exe.exists():
            _start_detached(exe)
        return 1
    for p in (sha_arg, sig_arg):
        try:
            os.unlink(p)
        except OSError:
            pass

    _wait_port_free(port)
    proc = _start_detached(exe)
    deadline = time.monotonic() + 30
    ok = False
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            break
        if _healthz(port):
            ok = True
            break
        time.sleep(1)
    if ok:
        log.info("selfupdate: обновление применено, healthz зелёный")
        _refresh_icon_cache()
        return 0

    log.warning("selfupdate: новая версия не поднялась — откат на prev")
    try:
        proc.kill()
    except Exception:
        pass
    try:
        _retry_fileop(lambda: exe.unlink(missing_ok=True))
        _retry_fileop(lambda: os.replace(prev, exe))
        _wait_port_free(port)
        _start_detached(exe)
        log.info("selfupdate: откат выполнен")
    except OSError as e:
        log.error("selfupdate: ОТКАТ НЕ УДАЛСЯ после ретраев: %r "
                  "(восстанови %s → %s вручную)", e, prev, exe)
    return 1









def _update_state_path(data_dir) -> Path:
    return Path(data_dir) / UPDATE_STATE_NAME


def read_update_state(data_dir) -> dict:

    try:
        st = json.loads(_update_state_path(data_dir).read_text(encoding="utf-8"))
        if isinstance(st, dict) and st.get("version"):
            return st
    except Exception:
        pass
    return {}


def _write_update_state(data_dir, version: str, first_seen: float) -> None:
    f = _update_state_path(data_dir)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps({"version": version, "first_seen": first_seen}),
                       encoding="utf-8")
        os.replace(tmp, f)
    except OSError as e:
        log.warning("selfupdate: не записать %s: %r", f, e)






_ARTIFACT_FILE_RE = re.compile(
    r"^maxgraph-(?P<ver>\d+(?:\.\d+){0,2})-(?:windows|macos|linux)-(?:amd64|arm64)"
    r"(?:\.exe)?(?:\.(?:sha256|sig|part))?$")


def cleanup_updates(data_dir, keep: int = 2) -> int:



    upd = Path(data_dir) / "updates"
    try:
        entries = [(m.group("ver"), p) for p in upd.iterdir() if p.is_file()
                   for m in [_ARTIFACT_FILE_RE.match(p.name)] if m]
    except OSError as e:
        log.warning("selfupdate: cleanup: не прочитать %s: %r", upd, e)
        return 0
    versions = sorted({v for v, _ in entries},
                      key=lambda v: _parse_semver(v) or (), reverse=True)
    stale = set(versions[keep:])
    removed = 0
    for v, p in entries:
        if v not in stale:
            continue
        try:
            p.unlink()
            removed += 1
        except OSError as e:
            log.warning("selfupdate: cleanup: не удалить %s: %r", p, e)
    if removed:
        log.info("selfupdate: cleanup: удалено %s файлов старых версий из %s", removed, upd)
    return removed






_notified: set = set()


def _download_apply_exit(store_url, version, data_dir) -> bool:


    path = download_and_verify(store_url, version, Path(data_dir) / "updates")
    if not path or not apply_update(path):
        return False
    time.sleep(1)
    os._exit(0)
    return True


def apply_pending_update(store_url, data_dir):




    if not _store_url_allowed(store_url):
        log.warning("selfupdate: store_url не https и не loopback — обновление отклонено")
        return False
    version = read_update_state(data_dir).get("version")
    if not version:
        return False
    if not _parse_semver(version):
        log.warning("selfupdate: в update-state мусорная версия %r — отказ", version)
        return False
    log.info("selfupdate: пользователь подтвердил обновление до %s", version)
    if not native_ready(store_url, version):
        log.info("selfupdate: %s объявлена, но native-артефакт ещё не раздаётся — "
                 "откладываю (гейт манифеста)", version)
        return UPDATE_PENDING
    return _download_apply_exit(store_url, version, data_dir)


def _update_iteration(get_version, store_url, data_dir, on_update=None, now=None) -> None:



    info = check(store_url, get_version())
    latest = info.get("version", "")
    if not (latest and info.get("newer")):
        return
    now = time.time() if now is None else now
    st = read_update_state(data_dir)
    if st.get("version") != latest:
        st = {"version": latest, "first_seen": now}
        _write_update_state(data_dir, latest, now)
    if latest not in _notified:
        _notified.add(latest)
        log.info("selfupdate: доступна %s (текущая %s) — жду подтверждения "
                 "(авто через 7д)", latest, get_version())
        if on_update is not None:
            try:
                on_update(latest)
            except Exception as e:
                log.warning("selfupdate: уведомление об обновлении: %r", e)
    try:
        first_seen = float(st.get("first_seen") or now)
    except (TypeError, ValueError):
        log.warning("selfupdate: first_seen=%r невалиден — отсчёт заново с сейчас",
                    st.get("first_seen"))
        first_seen = now
        _write_update_state(data_dir, latest, now)
    if now - first_seen < FALLBACK_AFTER_SEC:
        return
    log.info("selfupdate: %s ждёт подтверждения более 7д — ставлю автоматически", latest)
    _download_apply_exit(store_url, latest, data_dir)


def update_loop(get_version, store_url, data_dir, port, interval=None, on_update=None) -> None:



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
                    marker.unlink()
                    log.info("selfupdate: триггер .update-now — проверяю сейчас")
                    break
            except OSError as e:
                log.warning("selfupdate: маркер: %r", e)
            time.sleep(min(5, max(1, due - time.monotonic())))
        try:
            _update_iteration(get_version, store_url, data_dir, on_update)
        except Exception as e:
            log.warning("selfupdate: итерация цикла: %r", e)


def _store_url_allowed(store_url) -> bool:



    try:
        u = urllib.parse.urlparse(str(store_url or "").strip())
        host = (u.hostname or "").lower()
    except ValueError:
        return False
    if u.scheme == "https" and host:
        return True
    return u.scheme == "http" and host in ("localhost", "127.0.0.1")


def start_update_loop(get_version, store_url, data_dir, port, on_update=None):






    if os.environ.get("MG_NO_UPDATE") == "1":
        log.info("selfupdate: MG_NO_UPDATE=1 — автообновление выключено")
        return None
    if not _store_url_allowed(store_url):
        log.warning("selfupdate: store_url не https и не loopback — автообновление отключено")
        return None
    cleanup_updates(data_dir)
    t = threading.Thread(target=update_loop, args=(get_version, store_url, data_dir, port),
                         kwargs={"on_update": on_update}, daemon=True, name="selfupdate")
    t.start()
    return t
