#!/usr/bin/env python3
"""Нативный клиент MaxGraph без Docker: системный трей (pystray) + автозапуск ОС.

Stdlib-only на уровне импорта: pystray/PIL импортируются ЛЕНИВО внутри run_tray() —
серверный запуск (Docker / python app/app.py) не должен зависеть от GUI-библиотек.

Автозапуск (ensure_autostart, идемпотентно; ошибки — log.warning, сервер не роняем):
  Windows — HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run: "MaxGraph" = "<путь exe>"
  macOS   — ~/Library/LaunchAgents/ru.rezonai.maxgraph.plist (RunAtLoad=true)
  Linux   — ~/.config/autostart/maxgraph.desktop (XDG autostart)
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape
import json

log = logging.getLogger("maxgraph.native")

APP_NAME = "MaxGraph"
_MACOS_LABEL = "ru.rezonai.maxgraph"
_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ----------------------------------------------------------- билдеры (чистые)

def _exe_path() -> str:
    return str(Path(sys.executable).resolve())


def _run_key_value(exe: str | None = None) -> str:
    """Значение Run-ключа Windows: путь к exe в кавычках (пробелы в пути)."""
    exe = exe or _exe_path()
    return f'"{exe}"'


def _plist_xml(exe: str | None = None) -> str:
    """LaunchAgent plist для macOS."""
    exe = exe or _exe_path()
    exe = _xml_escape(exe)  # путь с & < > иначе ломает plist XML
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_MACOS_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
"""


def _desktop_entry(exe: str | None = None) -> str:
    """XDG .desktop для автозапуска на Linux."""
    exe = exe or _exe_path()
    return f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Exec={exe}
X-GNOME-Autostart-enabled=true
NoDisplay=true
"""


def _autostart_path(platform: str, home: Path) -> Path:
    if platform == "darwin":
        return home / "Library" / "LaunchAgents" / f"{_MACOS_LABEL}.plist"
    return home / ".config" / "autostart" / "maxgraph.desktop"


def _write_if_changed(path: Path, content: str) -> None:
    """Идемпотентная запись: пишем, только если записи нет или содержимое отличается."""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("autostart: записан %s", path)


# ------------------------------------------------------------------- Windows

def _win_ensure() -> None:
    import winreg
    value = _run_key_value()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0,
                        winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
        try:
            current, _ = winreg.QueryValueEx(key, APP_NAME)
        except FileNotFoundError:
            current = None
        if current != value:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, value)
            log.info("autostart: HKCU Run %s = %s", APP_NAME, value)


def _win_remove() -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        try:
            winreg.DeleteValue(key, APP_NAME)
            log.info("autostart: HKCU Run %s удалён", APP_NAME)
        except FileNotFoundError:
            pass  # уже нет — идемпотентно


# ----------------------------------------------------------------- ярлыки (Windows)

_SHORTCUT_DESC = "MaxGraph — память для AI-агентов"
_SHORTCUT_NAME = f"{APP_NAME}.lnk"


def _win_desktop_dir() -> Path:
    """KnownFolder Desktop из User Shell Folders: надёжнее ~\\Desktop —
    при OneDrive-редиректе «Рабочий стол» физически живёт не в профиле."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
        raw, _ = winreg.QueryValueEx(k, "Desktop")
    return Path(os.path.expandvars(raw))  # значение может быть REG_EXPAND_SZ (%USERPROFILE%)


def _win_start_menu_programs() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _shortcut_paths(desktop_dir=None, start_menu_dir=None) -> tuple[Path, Path]:
    """Оба места ярлыка; параметры — точка инъекции для тестов/установщика."""
    desktop_dir = Path(desktop_dir) if desktop_dir else _win_desktop_dir()
    start_menu_dir = Path(start_menu_dir) if start_menu_dir else _win_start_menu_programs()
    return desktop_dir / _SHORTCUT_NAME, start_menu_dir / _SHORTCUT_NAME


def _ps_quoted(s: str) -> str:
    """Одинарные кавычки PowerShell: без интерполяции $ и бэктиков, ' дублируется."""
    return "'" + s.replace("'", "''") + "'"


def _win_write_shortcut(link: Path, exe: str) -> None:
    """Создать/перезаписать .lnk через WScript.Shell COM (CreateShortcut).
    Вызов — powershell- subprocess: native.py остаётся stdlib-only при импорте
    (pywin32 в PyInstaller-сборке нет), та же техника, что в install.ps1."""
    link.parent.mkdir(parents=True, exist_ok=True)
    ps = ("$ws = New-Object -ComObject WScript.Shell; "
          f"$sc = $ws.CreateShortcut({_ps_quoted(str(link))}); "
          f"$sc.TargetPath = {_ps_quoted(exe)}; "
          f"$sc.WorkingDirectory = {_ps_quoted(str(Path(exe).parent))}; "
          f"$sc.IconLocation = {_ps_quoted(exe)}; "
          f"$sc.Description = {_ps_quoted(_SHORTCUT_DESC)}; "
          "$sc.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, timeout=30, check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ----------------------------------------------------------------- ярлыки (Linux/macOS)

def _linux_desktop_path(home: Path) -> Path:
    """XDG: меню приложений Linux."""
    return home / ".local" / "share" / "applications" / "maxgraph.desktop"


def _linux_desktop_entry(exe: str) -> str:
    """.desktop для меню приложений (не автозапуск — см. _desktop_entry)."""
    return f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Exec={exe}
Icon={exe}
Terminal=false
Categories=Utility
"""


def _linux_ensure_shortcut(home: Path, exe: str) -> None:
    path = _linux_desktop_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_linux_desktop_entry(exe), encoding="utf-8")  # (пере)записываем всегда
    log.info("shortcuts: записан %s -> %s", path, exe)


def _macos_apps_link(home: Path) -> Path:
    """«Пуск-аналог» macOS: симлинк в ~/Applications (Launchpad подхватывает его)."""
    return home / "Applications" / APP_NAME


def _macos_ensure_shortcut(home: Path, exe: str) -> None:
    """~/Applications/MaxGraph → exe (.app-бандл не собираем). Симлинк битый/чужой
    или на месте обычный файл — пересоздаём."""
    link = _macos_apps_link(home)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        try:
            if os.readlink(link) == exe:
                return  # уже правильный — идемпотентно
        except OSError:
            pass
        link.unlink()
    elif link.exists():
        link.unlink()
    os.symlink(exe, link)
    log.info("shortcuts: %s -> %s", link, exe)


def _remove_shortcut_path(path: Path) -> None:
    if path.is_symlink() or path.exists():  # is_symlink: ловит и БИТЫЕ ссылки (exists=False)
        path.unlink()
        log.info("shortcuts: удалён %s", path)


def ensure_shortcuts(platform: str | None = None, desktop_dir=None,
                     start_menu_dir=None, exe: str | None = None,
                     home: Path | None = None) -> bool:
    """Ярлык приложения в «меню» ОС:
      Windows — MaxGraph.lnk: рабочий стол + меню «Пуск» (WScript.Shell);
      Linux   — ~/.local/share/applications/maxgraph.desktop (перезаписываем всегда);
      macOS   — симлинк ~/Applications/MaxGraph → exe;
      прочие  — no-op True.
    Идемпотентно. Ошибки — log.warning, сервер не роняем.
    desktop_dir/start_menu_dir/home/exe — инъекция для тестов/установщика."""
    platform = sys.platform if platform is None else platform
    exe = exe or _exe_path()
    try:
        if platform.startswith("win"):
            for link in _shortcut_paths(desktop_dir, start_menu_dir):
                _win_write_shortcut(link, exe)
                log.info("shortcuts: записан %s -> %s", link, exe)
        elif platform == "darwin":
            _macos_ensure_shortcut(Path.home() if home is None else Path(home), exe)
        elif platform.startswith("linux"):
            _linux_ensure_shortcut(Path.home() if home is None else Path(home), exe)
        else:
            return True
        return True
    except Exception as exc:  # ярлыки не критичны — сервер живёт и без них
        log.warning("shortcuts: не удалось создать ярлыки: %s", exc)
        return False


def remove_shortcuts(platform: str | None = None, desktop_dir=None,
                     start_menu_dir=None, home: Path | None = None) -> bool:
    """Обратная операция (для uninstall): снять ярлык. True = успех/уже не было."""
    platform = sys.platform if platform is None else platform
    try:
        if platform.startswith("win"):
            for link in _shortcut_paths(desktop_dir, start_menu_dir):
                _remove_shortcut_path(link)
        elif platform == "darwin":
            _remove_shortcut_path(_macos_apps_link(Path.home() if home is None else Path(home)))
        elif platform.startswith("linux"):
            _remove_shortcut_path(_linux_desktop_path(Path.home() if home is None else Path(home)))
        else:
            return True
        return True
    except Exception as exc:
        log.warning("shortcuts: не удалось удалить ярлыки: %s", exc)
        return False


# ------------------------------------------------------------------ публичное

def ensure_autostart(platform: str | None = None, home: Path | None = None) -> bool:
    """Идемпотентная регистрация автозапуска текущего exe. True = успех (или уже было)."""
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else Path(home)
    try:
        if platform.startswith("win"):
            _win_ensure()
            return True
        if platform == "darwin":
            _write_if_changed(_autostart_path(platform, home), _plist_xml())
        else:
            _write_if_changed(_autostart_path(platform, home), _desktop_entry())
        return True
    except Exception as exc:  # автозапуск не критичен — сервер живёт и без него
        log.warning("autostart: не удалось зарегистрировать (%s): %s", platform, exc)
        return False


def remove_autostart(platform: str | None = None, home: Path | None = None) -> bool:
    """Обратная операция (для uninstall). True = успех (или уже не было)."""
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else Path(home)
    try:
        if platform.startswith("win"):
            _win_remove()
            return True
        path = _autostart_path(platform, home)
        if path.exists():
            path.unlink()
            log.info("autostart: удалён %s", path)
        return True
    except Exception as exc:
        log.warning("autostart: не удалось удалить (%s): %s", platform, exc)
        return False


# ------------------------------------------------------------------- hostfp

def _mac_str() -> str:
    return ":".join(f"{b:02x}" for b in uuid.getnode().to_bytes(6, "big"))


def _win_components() -> dict:
    """MachineGuid (реестр) + UUID/серийники через CIM (powershell)."""
    out = {}
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
            out["os_install_id"] = winreg.QueryValueEx(k, "MachineGuid")[0]
    except Exception as exc:
        log.warning("hostfp: MachineGuid недоступен: %s", exc)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_ComputerSystemProduct).UUID; "
             "(Get-CimInstance Win32_BaseBoard).SerialNumber; "
             "(Get-CimInstance Win32_DiskDrive | Select-Object -First 1).SerialNumber"],
            capture_output=True, text=True, timeout=20)
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        for tag, val in zip(("smbios_uuid", "board_serial", "disk_serial"), lines):
            out[tag] = val
    except Exception as exc:
        log.warning("hostfp: CIM недоступен: %s", exc)
    out["mac"] = _mac_str()
    return out


def _macos_components() -> dict:
    out = {}
    try:
        r = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                           capture_output=True, text=True, timeout=15)
        for line in r.stdout.splitlines():
            if "IOPlatformUUID" in line:
                out["os_install_id"] = line.split('"')[-2]
            elif "IOPlatformSerialNumber" in line:
                out["platform_serial"] = line.split('"')[-2]
    except Exception as exc:
        log.warning("hostfp: ioreg недоступен: %s", exc)
    out["mac"] = _mac_str()
    return out


def _linux_components() -> dict:
    out = {}
    for tag, path in (("os_install_id", "/etc/machine-id"),
                      ("smbios_uuid", "/sys/class/dmi/id/product_uuid"),
                      ("board_serial", "/sys/class/dmi/id/board_serial")):
        try:
            out[tag] = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    out["mac"] = _mac_str()
    return out


def _os_name(platform: str) -> str:
    return "windows" if platform.startswith("win") else ("macos" if platform == "darwin" else "linux")


def _collect_components(platform: str | None = None) -> dict:
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        return _win_components()
    if platform == "darwin":
        return _macos_components()
    return _linux_components()


def ensure_hostfp(data_dir=None, platform: str | None = None) -> bool:
    """hostfp.json рядом с данными, если его нет: exe сам собирает отпечаток
    (с машины уходят только хеши — как в install.sh/install.ps1)."""
    import hostfp
    platform = sys.platform if platform is None else platform
    if data_dir is None:
        import app as _app
        data_dir = _app.DATA_DIR
    f = Path(data_dir) / "hostfp.json"
    if f.exists():
        return True
    try:
        fp = hostfp.compute(_collect_components(platform))
        if not fp.get("host_fp"):
            log.warning("hostfp: ни одного валидного компонента — установка останется unanchored")
            return False
        doc = {"v": 1, "host_fp": fp["host_fp"], "components": fp["components"],
               "os": _os_name(platform),
               "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        os.replace(tmp, f)
        log.info("hostfp: отпечаток хоста записан (%s)", f)
        return True
    except Exception as exc:  # привязка не критична — сервер живёт и без неё
        log.warning("hostfp: не удалось записать отпечаток: %s", exc)
        return False


# ------------------------------------------------------------------ heartbeat

HEARTBEAT_SEC = 6 * 3600  # раз в 6ч; MG_HEARTBEAT_SEC — для тестов


def _read_install_id(data_dir) -> str:
    """install_id из <data>/install.json (его пишет app._telemetry_ping через
    _install_id). Нет/битый → "" — heartbeat молча пропускает тик, НЕ генерирует
    (генерация — прерогатива install-ping, иначе два источника id)."""
    try:
        return str(json.loads((Path(data_dir) / "install.json")
                              .read_text(encoding="utf-8")).get("install_id") or "")
    except Exception:
        return ""


def _install_flavor(env=None) -> str:
    """frozen exe → "native"; иначе конвенция install-ping (app._telemetry_ping):
    INSTALL_FLAVOR установщика или autodetect по /.dockerenv."""
    try:
        import app as _app
        if getattr(_app, "_FROZEN", False):
            return "native"
    except ImportError:
        pass
    env = os.environ if env is None else env
    return ((env.get("INSTALL_FLAVOR") or "").strip()
            or ("docker" if os.path.exists("/.dockerenv") else "python"))


def heartbeat_tick(post_json, base_url, data_dir, get_version) -> bool:
    """Один анонимный heartbeat на магазин: {install_id, version, os, flavor, ts}.
    True = POST ушёл без исключения (ответ не разбираем — шлём и забываем).
    Без install_id/base_url — молчаливый пропуск (сети нет вообще)."""
    iid = _read_install_id(data_dir)
    if not iid or not (base_url or "").strip():
        return False
    post_json(base_url.rstrip("/") + "/api/telemetry/heartbeat",
              {"install_id": iid, "version": str(get_version()),
               "os": _os_name(sys.platform), "flavor": _install_flavor(),
               "ts": time.time()}, timeout=5)
    return True


def start_heartbeat(post_json, base_url, data_dir, get_version,
                    interval=None, stop=None):
    """Daemon-поток heartbeat'а (T7, эпик native-client): тик сразу, дальше раз в
    6ч (MG_HEARTBEAT_SEC для тестов). Телеметрия анонимная и всегда ON
    (v0.41.0) — выключателя нет. Fail-open: первый сбой — log.warning, повторные
    — log.debug (не спамим раз в 6ч), успех сбрасывает. Магазин (STORE_MODE)
    поток не стартует — гейт в app.main()."""
    if interval is None:
        try:
            interval = float(os.environ.get("MG_HEARTBEAT_SEC", "") or HEARTBEAT_SEC)
        except ValueError:
            interval = HEARTBEAT_SEC
    stop = stop or threading.Event()

    def _loop():
        warned = False
        while not stop.is_set():
            try:
                heartbeat_tick(post_json, base_url, data_dir, get_version)
                warned = False
            except Exception as exc:
                if warned:
                    log.debug("heartbeat: повторный сбой: %r", exc)
                else:  # урок W1: тишина ≠ зависание — один warning, не поток
                    log.warning("heartbeat: сбой отправки (дальше — debug): %r", exc)
                    warned = True
            stop.wait(interval)

    t = threading.Thread(target=_loop, daemon=True, name="heartbeat")
    t.start()
    return t


# ---------------------------------------------------------------------- трей

def _default_port() -> int:
    return int(os.environ.get("PORT", "8787"))


def _read_local_token() -> str:
    """Токен локального сервера из <data>/local_token.txt (пусто — если недоступен)."""
    try:
        import app as _app
        f = _app.DATA_DIR / "local_token.txt"
        if f.exists():
            return f.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _dashboard_url(port: int, token: str = "") -> str:
    """Дашборд принимает токен через # (dashboard.html: фрагмент не уходит на сервер)."""
    return f"http://localhost:{port}/app" + (f"#{token}" if token else "")


def request_update_now(data_dir=None) -> bool:
    """Пункт трея «Проверить обновления»: пустой маркер DATA_DIR/.update-now —
    selfupdate.update_loop слушает его внутри интервала и триггерится мгновенно
    (тот же канал, что у кнопки дашборда /api/update/trigger). True = маркер стоит."""
    try:
        if data_dir is None:
            import app as _app
            data_dir = _app.DATA_DIR
        (Path(data_dir) / ".update-now").touch()
        log.info("tray: .update-now создан — цикл обновлений проверит магазин сейчас")
        return True
    except Exception as exc:  # трей не должен падать от ФС-ошибки
        log.warning("tray: не удалось создать .update-now: %s", exc)
        return False


def _wait_forever(event_factory=threading.Event) -> None:
    """Держать main-поток живым навсегда (daemon-поток сервера умрёт вместе
    с процессом — поэтому при падении иконки main-поток НЕ должен выйти).
    event_factory — точка подмены для тестов."""
    event_factory().wait()


def run_tray(main_callable, port: int | None = None) -> None:
    """Иконка в трее + сервер в daemon-потоке.

    Блокирует main thread: pystray требует главный поток под иконку (особенно macOS).
    Падение pystray НЕ роняет сервер: main-поток уходит в headless-ожидание
    (_wait_forever) — сервер важнее иконки (helper обновления стартует exe
    detached, иконка в такой сессии может быть недоступна).
    """
    import pystray
    from PIL import Image, ImageDraw

    port = port or _default_port()

    def _icon_image():  # программная иконка, без файлов-ассетов: синий круг + буква M
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=(88, 101, 242, 255))
        d.text((22, 16), "M", fill=(255, 255, 255, 255))
        return img

    def _open_dashboard(icon=None, item=None):
        webbrowser.open(_dashboard_url(port, _read_local_token()))

    def _restart(icon=None, item=None):
        os.execv(sys.executable, [sys.executable])  # замена процесса; сервер поднимется заново

    def _quit(icon=None, item=None):
        try:
            import app as _app
            flush = getattr(_app, "_actr_flush_quiet", None)
            if flush is not None:
                flush()
        except Exception as exc:
            log.warning("tray quit: actr flush failed: %s", exc)
        # shortcut: uvicorn крутится в daemon-потоке — глушим жёстко, graceful shutdown нет
        os._exit(0)

    def _check_updates(icon=None, item=None):
        request_update_now()  # маркер DATA_DIR/.update-now — мгновенный триггер update_loop

    threading.Thread(target=main_callable, daemon=True, name="server").start()
    menu = pystray.Menu(
        pystray.MenuItem("Открыть дашборд", _open_dashboard, default=True),
        pystray.MenuItem("Проверить обновления", _check_updates),
        pystray.MenuItem("Перезапуск", _restart),
        pystray.MenuItem("Выход", _quit),
    )
    try:
        pystray.Icon(APP_NAME, _icon_image(), APP_NAME, menu).run()
    except Exception as exc:
        # сервер важнее иконки: main-поток НЕ выходит (daemon-сервер живёт)
        log.error("трей недоступен — сервер работает headless: %r", exc, exc_info=True)
        _wait_forever()
