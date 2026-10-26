"""Самообновление нативного exe-клиента по подписанному каналу магазина.

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
     текущий → maxgraph.prev.ext, new → текущий (все rename/move — с ретраями
     _retry_fileop: на Windows ОС/антивирус держит свежий exe секунды после
     смерти процесса, PermissionError 13), перед запуском ждём освобождения
     порта (_wait_port_free), запуск, healthcheck 30с
     (GET http://localhost:{PORT}/healthz), провал → откат prev → текущий
     (тоже с ретраями — машина не должна остаться без exe).
     prev.exe НЕ удаляется — это и есть механизм отката; живёт до следующего
     успешного обновления (осознанно не чиним).

Только stdlib. Все сетевые/ФС ошибки — log.warning, цикл живёт (fail-open
на скачивании, fail-CLOSED на криптографии: битая сумма/подпись → None).
MG_NO_UPDATE=1 — update-цикл не стартует. MG_UPDATE_INTERVAL_SEC — интервал
(для тестов; дефолт 3600).

Новая версия НЕ ставится сразу: update_loop пишет data/update-state.json и
зовёт on_update (балун + пункт трея «Обновить до …» в native.py); скачивание —
по клику (apply_pending_update) или автоматически через FALLBACK_AFTER_SEC (7д)
от ПЕРВОГО обнаружения версии.

Гейт манифеста (гонка релиза 2026-10-19): /api/version поднимается деплоем
РАНЬШЕ, чем CI собирает и подписывает native-бинарники. Без гейта клиент
предлагал «Обновить до vX.Y.Z», клик падал 404 и показывал ложное «Не удалось
установить обновление». Поэтому check() отвечает newer=True, только когда
манифест native-раздачи УЖЕ содержит артефакт этой версии (native_ready), а
apply_pending_update() при нераздающемся артефакте возвращает UPDATE_PENDING
(карточка «Обновление ещё готовится», не «Не удалось»). Недоступный/битый
манифест — fail-open (старое поведение): это инфраструктурный сбой, а не
гонка релиза, и 7-дневный автофолбэк не должен отключаться молча.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import logging.handlers
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

MAX_ARTIFACT_BYTES = 256 * 1024 * 1024  # как у updater.sh (pkeyutl читает в RAM)
_CHUNK = 1 << 16
_UPDATE_FLAG = "--mg-apply-update"
FALLBACK_AFTER_SEC = 7 * 24 * 3600  # автофолбэк: 7 дней без подтверждения пользователя
UPDATE_STATE_NAME = "update-state.json"
# apply_pending_update: версия объявлена, но native-артефакт ещё не раздаётся
# (гонка релиза) — это НЕ провал установки; UI показывает «Обновление ещё
# готовится» (update_popup.PENDING), а не «Не удалось установить обновление».
UPDATE_PENDING = "pending"

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
    [L]P == identity для всех точек
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
        return False  # small-order/identity pubkey или R
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

def fetch_native_manifest(store_url) -> dict:
    """GET {store}/api/releases/native/manifest → dict; сбой/мусор → {} (fail-open)."""
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
    """True, если УЖЕ ПОЛУЧЕННЫЙ манифест содержит артефакт версии для платформы."""
    name = artifact_name(version, platform, arch)
    if not name:
        return False
    arts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(arts, list):
        return False
    return any(isinstance(a, dict) and a.get("name") == name for a in arts)


def native_ready(store_url, version, platform=None, arch=None) -> bool:
    """True, если магазин РЕАЛЬНО раздаёт native-артефакт этой версии для этой
    платформы. Гейт против гонки релиза: /api/version поднимается деплоем
    раньше, чем CI собирает и подписывает бинарники, — без гейта клиент
    предлагал обновление, клик падал 404 и показывал ложное «Не удалось
    установить обновление» (2026-10-19, v1.2.2).
    Различаем (security-ревизия v1.2.3, MAJOR — манифест не должен быть единой
    точкой отказа канала обновлений): ПОЛУЧЕННЫЙ манифест без артефакта → False
    (гонка релиза — ждём); манифест недоступен/мусор (инфраструктурный сбой) →
    True (fail-open, старое поведение: предлагаем; реальный 404 при скачивании
    честно покажет состояние, а 7-дневный автофолбэк не отключается молча)."""
    name = artifact_name(version, platform, arch)
    if not name:
        return False  # неизвестная платформа — раздавать нечего, сеть не дёргаем
    m = fetch_native_manifest(store_url)
    if not m:
        return True  # fail-open: сломанный эндпоинт ≠ «обновления нет»
    return _manifest_has_artifact(m, version, platform, arch)


def check(store_url, current) -> dict:
    """GET {store}/api/version → {"version": ..., "newer": bool}; сбой → {} (fail-open).
    newer=True — только когда магазин уже РАЗДАЁТ native-артефакт этой версии
    (гейт native_ready); версия объявлена, но ещё не раздаётся →
    {"newer": False, "native_pending": True} — обновление не предлагаем.
    Недоступный манифест — fail-open (см. native_ready)."""
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
        # CREATE_NO_WINDOW (0x08000000) вдобавок к DETACHED_PROCESS: формально
        # Windows игнорирует его вместе с DETACHED_PROCESS, но это страховка на
        # случай дочернего процесса console-системы (старые билды exe без
        # --windowed, python.exe из исходников) — консольное окно не должно
        # мелькать у пользователя ни при автозапуске, ни при обновлении
        # (кейс покупателя: кнопка обновления открывала консоль helper'а).
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
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
        # helper = КОПИЯ нового exe (updates/.helper.exe): свежий код helper'а,
        # и — критично — onefile читает lazy-модули из СВОЕГО exe-файла по пути
        # запуска; helper НЕ может жить в файле, который сам подменяет
        # (self-move нового exe → FileNotFoundError/zlib в helper'е).
        helper_exe = str(Path(new).with_name(".helper.exe"))
        shutil.copy2(new, helper_exe)  # перезаписывает копию прошлого обновления
        argv = [helper_exe, _UPDATE_FLAG, new, str(os.getpid())]
        sha, sig = new + ".sha256", new + ".sig"
        if os.path.exists(sha) and os.path.exists(sig):
            argv += [sha, sig]
        argv.append(exe)  # цель подмены — явно (helper запущен не из цели!)
        _spawn_detached(argv)
        log.info("selfupdate: helper запущен (%s → %s)", new_exe_path, exe)
        return True
    except Exception as e:
        log.warning("selfupdate: helper не запустился: %r", e)
        return False


def _healthz(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/healthz", timeout=timeout) as r:
            return r.read(4096).strip() == b"ok"  # строгое равенство
    except Exception:
        return False


def _start_detached(exe: Path):
    """Запуск exe ВНЕ дерева процессов helper'а. Windows: os.startfile (ShellExecute) —
    дети DETACHED-спавна умирали вместе с завершающимся helper'ом (подмена проходила, новый exe не переживал выход helper'а). Unix: как раньше."""
    if sys.platform == "win32":
        os.startfile(str(exe))  # noqa: S606 — путь наш, подпись проверена выше
        return None
    return _spawn_detached([str(exe)])


def _refresh_icon_cache() -> None:
    """Сброс кэша иконок Windows после подмены exe на ТОМ ЖЕ пути.

    Windows кэширует иконки по пути файла (iconcache): ярлыки на рабочем
    столе/в меню Пуск после самообновления показывают СТАРУЮ иконку, пока
    кэш не перечитают. ie4uinit.exe -show делает это без перезагрузки.
    Вызывается helper'ом один раз после успешной подмены (новый exe уже
    поднялся, healthz зелёный) — поэтому маркер/условие не нужны, при
    обычных стартах не дёргается. Fire-and-forget: ie4uinit может
    отсутствовать/упасть — любые ошибки глушим warning'ом, обновление
    из-за иконки неудачным не считается. Не-Windows — no-op.
    """
    if sys.platform != "win32":
        return
    # Абсолютный путь через SystemRoot: голое "ie4uinit.exe" CreateProcess ищет
    # СНАЧАЛА в каталоге процесса (helper живёт в user-writable DATA_DIR/updates)
    # и CWD — подброшенный туда неподписанный ie4uinit.exe исполнился бы в обход
    # ed25519-гейта обновления. System32 — системный каталог, не user-writable.
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
    """Файловая операция с ретраями на OSError: на Windows ОС/антивирус держит
    свежий exe несколько секунд после смерти процесса (PermissionError 13 на
    rename/move). Промежуточные фейлы — log.warning; исчерпание attempts —
    последнее исключение наружу (откат обязан знать о неудаче)."""
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
    """Ждать освобождения localhost:port до timeout секунд: старый процесс
    мёртв, но ОС может ещё держать его сокет — новый exe иначе не сбиндится.
    Свободен = connect отказан И bind удаётся. False по таймауту (запускаем
    всё равно — дальше решит healthcheck)."""
    deadline = time.monotonic() + timeout
    while True:
        busy = False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                busy = True  # кто-то ещё слушает порт
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
    """DATA_DIR как в app.py (_default_data_dir): MEMORY_DIR, иначе ./data
    рядом с exe. Helper наследует env и cwd родителя → относительный
    MEMORY_DIR резолвится так же, как у родителя."""
    env_dir = os.environ.get("MEMORY_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    return Path(sys.executable).resolve().parent / "data"


def _setup_helper_log(target_exe=None) -> None:
    """Лог helper'а в data/update.log ЦЕЛЕВОГО exe: stdout detached-процесса
    нечитаем — такие сбои иначе диагностируются вслепую. Только в frozen exe.
    (helper запущен из НОВОГО exe в updates/ — считать data от цели!)"""
    if not getattr(sys, "frozen", False):
        return
    try:
        base = Path(target_exe).resolve().parent / "data" if target_exe else _helper_data_dir()
        # RotatingFileHandler (аудит #30): рос без ротации; 1 МБ × 2, как maxgraph.log
        fh = logging.handlers.RotatingFileHandler(base / "update.log", maxBytes=1_000_000,
                                                  backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
    except OSError:
        pass


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
        log.warning("selfupdate: helper: перепроверка sidecar'ов: %r (файл: %s)",
                    e, getattr(e, "filename", "?"))
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
    # цель подмены: явный argv[4] (helper запущен из НОВОГО exe,
    # sys.executable ≠ цель); старый формат — цель = свой exe
    target_exe = argv[4] if len(argv) > 4 else None
    _setup_helper_log(target_exe)
    # (а) pid обязан быть положительным числом — никакой «подмены сразу»
    try:
        parent_pid = int(pid_arg)
    except (TypeError, ValueError):
        parent_pid = -1
    if parent_pid <= 0:
        log.error("selfupdate: helper: невалидный parent_pid %r — отказ", pid_arg)
        return 2
    # (б) new обязан лежать внутри updates ЦЕЛЕВОГО exe — чужие пути отклоняем
    # (helper живёт в updates сам — считать data от target, не от себя)
    target_dir = (Path(target_exe).resolve().parent / "data") if target_exe else _helper_data_dir()
    try:
        allowed = (target_dir / "updates").resolve()
        new_path = Path(new_arg).resolve()
        new_path.relative_to(allowed)  # ValueError → вне updates
    except (ValueError, OSError) as e:
        log.error("selfupdate: helper: %s вне DATA_DIR/updates — отказ (%r)", new_arg, e)
        return 2
    exe = Path(target_exe).resolve() if target_exe else Path(sys.executable).resolve()
    # prev — механизм отката (НЕ удалять; живёт до следующего
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
        _start_detached(exe)  # родитель уже мёртв — поднимаем целый exe обратно
        return 2
    try:
        # rename бегущего exe на Windows разрешён, но ОС/антивирус может
        # держать файлы секунды после смерти родителя → ретраи
        _retry_fileop(lambda: os.replace(exe, prev))
        _retry_fileop(lambda: shutil.move(str(new_path), exe))
    except OSError as e:
        log.error("selfupdate: подмена не удалась после ретраев: %r", e)
        try:
            if not exe.exists() and prev.exists():
                _retry_fileop(lambda: os.replace(prev, exe))  # не оставляем машину без exe
        except OSError as e2:
            log.error("selfupdate: ОТКАТ подмены не удался: %r "
                      "(восстанови %s → %s вручную)", e2, prev, exe)
        if exe.exists():
            _start_detached(exe)  # родитель мёртв — поднимаем что есть
        return 1
    for p in (sha_arg, sig_arg):       # sidecar'ы отработали — чистим
        try:
            os.unlink(p)
        except OSError:
            pass

    _wait_port_free(port)  # сокет умершего родителя может ещё держать порт
    proc = _start_detached(exe)
    deadline = time.monotonic() + 30
    ok = False
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            break  # новый exe упал сразу
        if _healthz(port):
            ok = True
            break
        time.sleep(1)
    if ok:
        log.info("selfupdate: обновление применено, healthz зелёный")
        _refresh_icon_cache()  # Windows: exe подменён на том же пути — сбросить iconcache
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


# ---------------------------------------------------------------------------
# Стейт ожидающего обновления (data/update-state.json): момент ПЕРВОГО
# обнаружения версии персистим, чтобы перезапуск не сбрасывал 7-дневный отсчёт.
# Этот же файл читает трей (пункт «Обновить до …») — один источник правды,
# без межпоточных объектов; headless-режим просто не читает его.
# ---------------------------------------------------------------------------

def _update_state_path(data_dir) -> Path:
    return Path(data_dir) / UPDATE_STATE_NAME


def read_update_state(data_dir) -> dict:
    """{"version": ..., "first_seen": epoch} или {} — битый/отсутствующий файл не страшен."""
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
        os.replace(tmp, f)  # атомарно: трей может читать файл в этот момент
    except OSError as e:
        log.warning("selfupdate: не записать %s: %r", f, e)


# ---------------------------------------------------------------------------
# Чистка data/updates: остаются файлы двух СВЕЖИХ версий (текущая + предыдущая).
# ---------------------------------------------------------------------------

_ARTIFACT_FILE_RE = re.compile(
    r"^maxgraph-(?P<ver>\d+(?:\.\d+){0,2})-(?:windows|macos|linux)-(?:amd64|arm64)"
    r"(?:\.exe)?(?:\.(?:sha256|sig|part))?$")


def cleanup_updates(data_dir, keep: int = 2) -> int:
    """Удалить файлы версий старше keep свежих из data/updates. → число удалённых.
    .helper.exe (подменщик exe при apply) и неартефактные файлы НЕ трогаем;
    maxgraph.prev.exe живёт в корне установки, сюда не входит. Ошибки — warning.
    Аудит 2026-10-25 (#29): дополнительно чистим сирот — updates/data/ (мусорный
    каталог старых helper'ов, там залёгли СТАРЫЕ секреты) и оборванные
    maxgraph-*.part в корне установки (вне data/updates их никто не чистил)."""
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
    # updates/data/ — легитимно не создаётся (helper пишет лог в data/ ЦЕЛИ);
    # залёгшая копия — мусор со старыми токенами (аудит B)
    upd_data = upd / "data"
    if upd_data.is_dir():
        try:
            shutil.rmtree(upd_data)
            removed += 1
            log.info("selfupdate: cleanup: удалён мусорный %s", upd_data)
        except OSError as e:
            log.warning("selfupdate: cleanup: не удалить %s: %r", upd_data, e)
    # оборванные .part-закачки в КОРНЕ установки (data_dir = <install>/data)
    try:
        for p in Path(data_dir).resolve().parent.glob("maxgraph-*.part"):
            try:
                p.unlink()
                removed += 1
                log.info("selfupdate: cleanup: удалён сирота %s", p)
            except OSError as e:
                log.warning("selfupdate: cleanup: не удалить %s: %r", p, e)
    except OSError:
        pass  # корень нечитаем — не критично
    return removed


# ---------------------------------------------------------------------------
# Фоновый цикл
# ---------------------------------------------------------------------------

_notified: set = set()  # версии, о которых уведомили в ЭТОМ процессе (балун — не каждый час)


def _download_apply_exit(store_url, version, data_dir) -> bool:
    """Скачать/проверить/применить version; успех → os._exit(0) (helper подменит
    exe и перезапустит). False — скачивание/запуск helper'а не удался, живём дальше."""
    path = download_and_verify(store_url, version, Path(data_dir) / "updates")
    if not path or not apply_update(path):
        return False
    time.sleep(1)   # дать helper'у подняться до смерти родителя
    os._exit(0)     # helper ждёт parent_pid, подменит и перезапустит
    return True     # unreachable без подмены os._exit (тесты)


def apply_pending_update(store_url, data_dir):
    """Клик по пункту трея «Обновить до …»: поставить версию из update-state.json.
    True — helper запущен и процесс умирает; UPDATE_PENDING — версия объявлена,
    но native-артефакт ещё не раздаётся (гонка релиза, не провал); False —
    нечего ставить / сбой."""
    if not _store_url_allowed(store_url):  # тот же гард, что у start_update_loop:
        log.warning("selfupdate: store_url не https и не loopback — обновление отклонено")
        return False                       # ручной путь (apply_update_from_ui) не обходит его
    version = read_update_state(data_dir).get("version")
    if not version:
        return False
    if not _parse_semver(version):  # version из файла идёт в artifact_name → URL магазина
        log.warning("selfupdate: в update-state мусорная версия %r — отказ", version)
        return False
    log.info("selfupdate: пользователь подтвердил обновление до %s", version)
    if not native_ready(store_url, version):  # объявлена ≠ раздаётся: 404 ≠ провал
        log.info("selfupdate: %s объявлена, но native-артефакт ещё не раздаётся — "
                 "откладываю (гейт манифеста)", version)
        return UPDATE_PENDING
    return _download_apply_exit(store_url, version, data_dir)


def _update_iteration(get_version, store_url, data_dir, on_update=None, now=None) -> None:
    """Одна проверка магазина. Новая версия → стейт + уведомление (on_update);
    скачивание — ТОЛЬКО по клику (apply_pending_update) или автоматически,
    когда версия ждёт подтверждения дольше FALLBACK_AFTER_SEC."""
    info = check(store_url, get_version())
    latest = info.get("version", "")
    if not (latest and info.get("newer")):
        return
    now = time.time() if now is None else now
    st = read_update_state(data_dir)
    if st.get("version") != latest:  # первое обнаружение ЭТОЙ версии — отсчёт с нуля
        st = {"version": latest, "first_seen": now}
        _write_update_state(data_dir, latest, now)
    if latest not in _notified:
        _notified.add(latest)
        log.info("selfupdate: доступна %s (текущая %s) — жду подтверждения "
                 "(авто через 7д)", latest, get_version())
        if on_update is not None:
            try:
                on_update(latest)
            except Exception as e:  # балун/трей не должны ронять цикл
                log.warning("selfupdate: уведомление об обновлении: %r", e)
    try:
        first_seen = float(st.get("first_seen") or now)
    except (TypeError, ValueError):  # мусорный first_seen: ValueError каждую итерацию
        log.warning("selfupdate: first_seen=%r невалиден — отсчёт заново с сейчас",
                    st.get("first_seen"))
        first_seen = now
        _write_update_state(data_dir, latest, now)  # лечим стейт, иначе warning повторится
    if now - first_seen < FALLBACK_AFTER_SEC:
        return
    log.info("selfupdate: %s ждёт подтверждения более 7д — ставлю автоматически", latest)
    _download_apply_exit(store_url, latest, data_dir)


def update_loop(get_version, store_url, data_dir, port, interval=None, on_update=None) -> None:
    """Daemon-цикл: раз в interval (env MG_UPDATE_INTERVAL_SEC, дефолт 3600)
    + по маркеру data_dir/.update-now: check → новее → стейт + уведомление;
    установка — по клику из трея или по 7-дневному фолбэку (_update_iteration)."""
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
            _update_iteration(get_version, store_url, data_dir, on_update)
        except Exception as e:
            log.warning("selfupdate: итерация цикла: %r", e)


def _store_url_allowed(store_url) -> bool:
    """https — всегда; http — ТОЛЬКО loopback netloc (localhost/127.0.0.1,
    с портом или без). Подстрока «localhost» пропускала
    http://localhost.evil.tld."""
    try:
        u = urllib.parse.urlparse(str(store_url or "").strip())
        host = (u.hostname or "").lower()
    except ValueError:
        return False
    if u.scheme == "https" and host:
        return True
    return u.scheme == "http" and host in ("localhost", "127.0.0.1")


def start_update_loop(get_version, store_url, data_dir, port, on_update=None):
    """Запустить daemon-поток обновлений. MG_NO_UPDATE=1 → не стартует (None).
    Открытый (не-https) канал до не-локального магазина → не стартует:
    подпись по открытому каналу бессмысленна (зеркало гарда updater.sh).
    on_update(version) — колбэк «есть новая версия» (балун трея; None в headless).
    Заодно при старте чистим data/updates от версий старше двух свежих
    (после успешного обновления это делает старт нового процесса)."""
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
