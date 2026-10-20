#!/usr/bin/env python3










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



_RESOURCE_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))




def _exe_path() -> str:
    return str(Path(sys.executable).resolve())


def _run_key_value(exe: str | None = None) -> str:

    exe = exe or _exe_path()
    return f'"{exe}"'


def _plist_xml(exe: str | None = None) -> str:

    exe = exe or _exe_path()
    exe = _xml_escape(exe)
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

    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("autostart: записан %s", path)




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
            pass




_SHORTCUT_DESC = "MaxGraph — память для AI-агентов"
_SHORTCUT_NAME = f"{APP_NAME}.lnk"


def _win_desktop_dir() -> Path:


    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
        raw, _ = winreg.QueryValueEx(k, "Desktop")
    return Path(os.path.expandvars(raw))


def _win_start_menu_programs() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _shortcut_paths(desktop_dir=None, start_menu_dir=None) -> tuple[Path, Path]:

    desktop_dir = Path(desktop_dir) if desktop_dir else _win_desktop_dir()
    start_menu_dir = Path(start_menu_dir) if start_menu_dir else _win_start_menu_programs()
    return desktop_dir / _SHORTCUT_NAME, start_menu_dir / _SHORTCUT_NAME


def _ps_quoted(s: str) -> str:

    return "'" + s.replace("'", "''") + "'"


def _win_write_shortcut(link: Path, exe: str) -> None:



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




def _linux_desktop_path(home: Path) -> Path:

    return home / ".local" / "share" / "applications" / "maxgraph.desktop"


def _linux_desktop_entry(exe: str) -> str:

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
    path.write_text(_linux_desktop_entry(exe), encoding="utf-8")
    log.info("shortcuts: записан %s -> %s", path, exe)


def _macos_apps_link(home: Path) -> Path:

    return home / "Applications" / APP_NAME


def _macos_ensure_shortcut(home: Path, exe: str) -> None:


    link = _macos_apps_link(home)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        try:
            if os.readlink(link) == exe:
                return
        except OSError:
            pass
        link.unlink()
    elif link.exists():
        link.unlink()
    os.symlink(exe, link)
    log.info("shortcuts: %s -> %s", link, exe)


def _remove_shortcut_path(path: Path) -> None:
    if path.is_symlink() or path.exists():
        path.unlink()
        log.info("shortcuts: удалён %s", path)


def ensure_shortcuts(platform: str | None = None, desktop_dir=None,
                     start_menu_dir=None, exe: str | None = None,
                     home: Path | None = None) -> bool:







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
    except Exception as exc:
        log.warning("shortcuts: не удалось создать ярлыки: %s", exc)
        return False


def remove_shortcuts(platform: str | None = None, desktop_dir=None,
                     start_menu_dir=None, home: Path | None = None) -> bool:

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




def ensure_autostart(platform: str | None = None, home: Path | None = None) -> bool:

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
    except Exception as exc:
        log.warning("autostart: не удалось зарегистрировать (%s): %s", platform, exc)
        return False


def remove_autostart(platform: str | None = None, home: Path | None = None) -> bool:

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




def _mac_str() -> str:
    return ":".join(f"{b:02x}" for b in uuid.getnode().to_bytes(6, "big"))


def _win_components() -> dict:

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
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
        log.info("hostfp: отпечаток хоста записан (%s)", f)
        return True
    except Exception as exc:
        log.warning("hostfp: не удалось записать отпечаток: %s", exc)
        return False




_HOOK_NAMES = ("maxgraph_hook.py", "register_hook.py")


def _hook_src_dir() -> Path:


    cand = _RESOURCE_BASE / "hooks"
    return cand if cand.is_dir() else _RESOURCE_BASE.parent / "hooks"


def _atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:





    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")

    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    created = False
    try:
        try:
            fd = os.open(tmp, flags, 0o600)
        except FileExistsError:
            tmp.unlink()
            fd = os.open(tmp, flags, 0o600)
        created = True
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        if created:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def ensure_hook_files(home=None, token: str = "", port: int = 8787) -> bool:








    try:
        home = Path.home() if home is None else Path(home)
        d = home / ".maxgraph"
        if d.is_symlink():
            log.warning("hooks: %s — symlink, отказ от записи", d)
            return False
        d.mkdir(parents=True, exist_ok=True)
        src = _hook_src_dir()
        for name in _HOOK_NAMES:
            try:

                s = src / name
                if not s.is_file():
                    log.warning("hooks: %s нет в ресурсах — пропуск", s)
                    continue
                data = s.read_bytes()
                t = d / name
                if not t.is_file() or t.read_bytes() != data:
                    _atomic_write(t, data)
                    log.info("hooks: %s обновлён из ресурсов exe", t)
            except Exception as exc:
                log.warning("hooks: %s не обновлён: %s", name, exc)
    except Exception as exc:
        log.warning("hooks: ensure_hook_files не удался: %s", exc)
        return False
    try:


        tok = "".join(str(token or "").split())
        if tok:
            _atomic_write(d / "hooks.env",
                          f"MG_SERVER=http://localhost:{port}\nMG_TOKEN={tok}\n"
                          .encode("utf-8"), mode=0o600)
    except Exception as exc:
        log.warning("hooks: hooks.env не записан: %s", exc)
        return False
    return True




HEARTBEAT_SEC = 6 * 3600


def _read_install_id(data_dir) -> str:



    try:
        return str(json.loads((Path(data_dir) / "install.json")
                              .read_text(encoding="utf-8")).get("install_id") or "")
    except Exception:
        return ""


def _install_flavor(env=None) -> str:


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
                else:
                    log.warning("heartbeat: сбой отправки (дальше — debug): %r", exc)
                    warned = True
            stop.wait(interval)

    t = threading.Thread(target=_loop, daemon=True, name="heartbeat")
    t.start()
    return t




def _default_port() -> int:
    return int(os.environ.get("PORT", "8787"))


def _read_local_token() -> str:

    try:
        import app as _app
        f = _app.DATA_DIR / "local_token.txt"
        if f.exists():
            return f.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _dashboard_url(port: int, token: str = "") -> str:

    return f"http://localhost:{port}/app" + (f"#{token}" if token else "")


def open_dashboard(port: int | None = None) -> None:


    webbrowser.open(_dashboard_url(port or _default_port(), _read_local_token()))


def request_update_now(data_dir=None) -> bool:



    try:
        if data_dir is None:
            import app as _app
            data_dir = _app.DATA_DIR
        (Path(data_dir) / ".update-now").touch()
        log.info("tray: .update-now создан — цикл обновлений проверит магазин сейчас")
        return True
    except Exception as exc:
        log.warning("tray: не удалось создать .update-now: %s", exc)
        return False




_tray_icon = None


def _pending_update(data_dir=None) -> str:


    try:
        import selfupdate
        if data_dir is None:
            import app as _app
            data_dir = _app.DATA_DIR
        version = str(selfupdate.read_update_state(data_dir).get("version") or "")
        if not version:
            return ""
        import app as _app

        return version if selfupdate.semver_newer(version, _app.VERSION) else ""
    except Exception:
        return ""


def _balloon(msg: str) -> None:


    icon = _tray_icon
    if icon is None:
        return
    try:
        icon.notify(msg, APP_NAME)
    except Exception as exc:
        log.warning("tray: балун недоступен: %r", exc)


def notify_update_available(version: str) -> None:



    _balloon(f"Доступно обновление MaxGraph v{version}")
    icon = _tray_icon
    if icon is None:
        return
    try:
        icon.update_menu()
    except Exception:
        pass


def _refresh_menu() -> None:

    icon = _tray_icon
    if icon is None:
        return
    try:
        icon.update_menu()
    except Exception:
        pass


def apply_update_from_ui():




    try:
        import selfupdate
        import app as _app
        res = selfupdate.apply_pending_update(_app.PUBLIC_BASE_URL, _app.DATA_DIR)
        if not res:
            log.warning("tray: обновление не запустилось (нет стейта или сбой скачивания)")
        return res
    except Exception as exc:
        log.warning("tray: apply обновления: %r", exc)
        return False


def _remember_update(version: str) -> None:



    try:
        import selfupdate
        import app as _app
        if selfupdate.read_update_state(_app.DATA_DIR).get("version") != version:
            selfupdate._write_update_state(_app.DATA_DIR, version, time.time())
    except Exception as exc:
        log.warning("tray: не удалось записать update-state: %r", exc)


def _check_updates_worker() -> None:



    import update_popup
    try:
        import selfupdate
        import app as _app
        info = selfupdate.check(_app.PUBLIC_BASE_URL, _app.VERSION)
        current = _app.VERSION
    except Exception as exc:
        log.warning("tray: проверка обновлений: %r", exc)
        info, current = {}, ""
    state, version = update_popup.state_from_check(info)
    on_update = None
    if state == update_popup.UPDATE:
        _remember_update(version)
        _refresh_menu()
        on_update = apply_update_from_ui
    elif state == update_popup.LATEST:
        version = current
    if not update_popup.update_or_show(state, version=version, on_update=on_update):
        _balloon(update_popup.view_for(state, version)["title"])


def check_updates_now() -> None:



    import update_popup
    if os.environ.get("MG_NO_UPDATE") == "1":
        if not update_popup.update_or_show(update_popup.DISABLED):
            _balloon(update_popup.view_for(update_popup.DISABLED)["title"])
        return
    request_update_now()
    update_popup.update_or_show(update_popup.CHECKING)
    threading.Thread(target=_check_updates_worker, daemon=True, name="update-check").start()


def _wait_forever(event_factory=threading.Event) -> None:



    event_factory().wait()


def _tray_icon_image():



    from PIL import Image, ImageDraw

    png = _RESOURCE_BASE / "assets" / "tray-icon.png"
    try:
        img = Image.open(png)
        img.load()
        return img
    except Exception as exc:
        log.warning("tray icon: не загрузился %s (%r) — программная отрисовка", png, exc)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(88, 101, 242, 255))
    d.text((22, 16), "M", fill=(255, 255, 255, 255))
    return img


def run_tray(main_callable, port: int | None = None) -> None:







    import pystray

    port = port or _default_port()

    def _open_dashboard(icon=None, item=None):
        webbrowser.open(_dashboard_url(port, _read_local_token()))

    def _restart(icon=None, item=None):
        os.execv(sys.executable, [sys.executable])

    def _quit(icon=None, item=None):
        try:
            import app as _app
            flush = getattr(_app, "_actr_flush_quiet", None)
            if flush is not None:
                flush()
        except Exception as exc:
            log.warning("tray quit: actr flush failed: %s", exc)

        os._exit(0)

    def _check_updates(icon=None, item=None):
        check_updates_now()

    def _apply_update(icon=None, item=None):

        apply_update_from_ui()

    def _update_visible(item=None) -> bool:

        return bool(_pending_update()) and os.environ.get("MG_NO_UPDATE") != "1"

    threading.Thread(target=main_callable, daemon=True, name="server").start()
    menu = pystray.Menu(
        pystray.MenuItem(lambda item: f"Обновить до v{_pending_update() or '?'}",
                         _apply_update, visible=_update_visible),
        pystray.MenuItem("Открыть дашборд", _open_dashboard, default=True),
        pystray.MenuItem("Проверить обновления", _check_updates),
        pystray.MenuItem("Перезапуск", _restart),
        pystray.MenuItem("Выход", _quit),
    )
    global _tray_icon
    icon = pystray.Icon(APP_NAME, _tray_icon_image(), APP_NAME, menu)
    _tray_icon = icon
    try:
        icon.run()
    except Exception as exc:
        _tray_icon = None

        log.error("трей недоступен — сервер работает headless: %r", exc, exc_info=True)
        _wait_forever()
