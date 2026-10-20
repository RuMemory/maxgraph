#!/usr/bin/env python3
# -*- coding: utf-8 -*-

































from __future__ import annotations

import logging
import queue
import sys
import threading

import selfupdate as _su

log = logging.getLogger("maxgraph.update_popup")

APP_TITLE = "MaxGraph"


CHECKING = "checking"
UPDATE = "update"
LATEST = "latest"
OFFLINE = "offline"
FAILED = "failed"
DISABLED = "disabled"
PENDING = _su.UPDATE_PENDING


AUTOHIDE_MS = 5000
CHECK_TIMEOUT_MS = 10000

_BG_TOP = "#272a32"
_BG_BOTTOM = "#3d414b"
_PLATE = "#1d2026"
_ACCENT = "#ffb08a"
_ACCENT_HOT = "#ff6b4a"
_ON_ACCENT = "#272a32"
_FG = "#f2f2f5"
_FG_DIM = "#b7bac3"

_W, _H = 380, 120

_demo_autoclose_ms = 0




def popup_supported(platform: str | None = None) -> bool:

    platform = sys.platform if platform is None else platform
    return platform.startswith("win")


def state_from_check(info: dict) -> tuple[str, str]:



    latest = str((info or {}).get("version") or "").strip()
    if not latest:
        return OFFLINE, ""
    if info.get("newer"):
        return UPDATE, latest
    if info.get("native_pending"):
        return PENDING, latest
    return LATEST, latest


def view_for(state: str, version: str = "") -> dict:




    if state == UPDATE:
        return {"title": f"Доступно обновление v{version}",
                "subtitle": "Нажмите «Обновить» для установки",
                "button": "Обновить", "autohide_ms": 0}
    if state == LATEST:
        return {"title": "У вас последняя версия",
                "subtitle": f"{APP_TITLE} v{version}" if version else APP_TITLE,
                "button": None, "autohide_ms": AUTOHIDE_MS}
    if state == OFFLINE:
        return {"title": "Нет связи с сервером",
                "subtitle": "Проверьте интернет и повторите позже",
                "button": None, "autohide_ms": AUTOHIDE_MS}
    if state == FAILED:
        return {"title": "Не удалось установить обновление",
                "subtitle": "Попробуйте позже",
                "button": None, "autohide_ms": AUTOHIDE_MS}
    if state == PENDING:
        return {"title": "Обновление ещё готовится",
                "subtitle": "Файл появится на сервере в ближайшее время",
                "button": None, "autohide_ms": AUTOHIDE_MS}
    if state == DISABLED:
        return {"title": "Обновления отключены",
                "subtitle": "Отключено в настройках (MG_NO_UPDATE)",
                "button": None, "autohide_ms": AUTOHIDE_MS}
    return {"title": "Проверяю обновления…",
            "subtitle": "Связываюсь с магазином",
            "button": None, "autohide_ms": 0}




_lock = threading.Lock()
_ctrl = None


def update_or_show(state: str, version: str = "", on_update=None) -> bool:





    if not popup_supported():
        return False
    global _ctrl
    with _lock:
        if _ctrl is not None:
            q, t = _ctrl
            if t.is_alive():
                q.put((state, version, on_update))
                return True
            _ctrl = None
        q = queue.Queue()
        ready = threading.Event()
        ok = []
        t = threading.Thread(target=_run_window,
                             args=(q, ready, ok, state, version, on_update),
                             daemon=True, name="update-popup")
        t.start()
        _ctrl = (q, t)
    ready.wait(5)
    if not ok:
        with _lock:
            _ctrl = None
        return False
    return True


def _hex_lerp(c1: str, c2: str, t: float) -> str:

    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def _round_rect(cv, x1, y1, x2, y2, r, **kw):

    p = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
         x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(p, smooth=True, **kw)


def _draw_logo(cv, x, y):


    _round_rect(cv, x, y, x + 40, y + 40, 9, fill=_PLATE, outline=_BG_BOTTOM)
    cx, cy = x + 20, y + 20
    cv.create_oval(cx - 14, cy - 7, cx + 14, cy + 7, outline=_ACCENT, width=1)
    cv.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, fill=_ACCENT_HOT, outline="")
    cv.create_oval(cx + 9, cy - 13, cx + 14, cy - 8, fill=_ACCENT, outline="")


def _hide(root) -> None:




    root.withdraw()


def _run_window(q, ready, ok, state, version, on_update) -> None:




    try:
        import tkinter as tk
    except Exception as exc:
        log.warning("update-popup: tkinter недоступен: %r", exc)
        ready.set()
        return
    root = None
    try:
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        x = root.winfo_screenwidth() - _W - 16
        y = root.winfo_screenheight() - _H - 56
        root.geometry(f"{_W}x{_H}+{x}+{y}")
        cv = tk.Canvas(root, width=_W, height=_H, highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)
        for i in range(_H):
            cv.create_line(0, i, _W, i, fill=_hex_lerp(_BG_TOP, _BG_BOTTOM, i / _H))


        _draw_logo(cv, 16, 38)
        cv.create_text(68, 34, anchor="w", fill=_FG, text=APP_TITLE,
                       font=("Segoe UI", 13, "bold"), width=268)
        status = cv.create_text(68, 62, anchor="w", fill=_FG,
                                font=("Segoe UI", 10), width=268)
        hint = cv.create_text(68, 86, anchor="w", fill=_FG_DIM,
                              font=("Segoe UI", 9), width=296)

        cv.create_rectangle(_W - 40, 0, _W, 40, fill="", outline="", tags="close")
        cv.create_text(_W - 20, 20, text="✕", fill=_FG_DIM, tags="close",
                       font=("Segoe UI", 11))
        cur = {"on_update": None, "after_id": None, "check_id": None,
               "busy": False, "hidden": False}

        def hide():
            cur["hidden"] = True
            _hide(root)

        cv.tag_bind("close", "<Button-1>", lambda e: hide())


        bx2, by2 = _W - 16, _H - 16
        bx1, by1 = bx2 - 104, by2 - 44
        btn_bg = _round_rect(cv, bx1, by1, bx2, by2, 10,
                             fill=_ACCENT_HOT, outline="", tags="btn")
        btn_tx = cv.create_text((bx1 + bx2) // 2, (by1 + by2) // 2, fill=_ON_ACCENT,
                                font=("Segoe UI", 9, "bold"), tags="btn")
        cv.itemconfigure("btn", state="hidden")

        def apply_state(s, v, cb):
            view = view_for(s, v)
            if cur["hidden"]:
                cur["hidden"] = False
                root.deiconify()
                root.focus_force()
            cv.itemconfigure(status, text=view["title"])
            cv.itemconfigure(hint, text=view["subtitle"],
                             width=180 if view["button"] else 296)
            cur["on_update"] = cb
            cur["busy"] = False
            for key in ("after_id", "check_id"):
                if cur[key] is not None:
                    root.after_cancel(cur[key])
                    cur[key] = None
            if view["button"]:
                cv.itemconfigure(btn_tx, text=view["button"])
                cv.itemconfigure("btn", state="normal")
            else:
                cv.itemconfigure("btn", state="hidden")
            if view["autohide_ms"]:
                cur["after_id"] = root.after(view["autohide_ms"], hide)
            elif s == CHECKING:
                cur["check_id"] = root.after(
                    CHECK_TIMEOUT_MS, lambda: apply_state(OFFLINE, "", None))

        def _on_button():
            cb = cur["on_update"]
            if cb is None or cur["busy"]:
                return
            cur["busy"] = True
            cv.itemconfigure("btn", state="hidden")
            cv.itemconfigure(hint, text="Скачиваю и устанавливаю…", width=296)

            def _do():
                try:
                    done = cb()
                except Exception as exc:
                    log.warning("update-popup: apply: %r", exc)
                    done = False
                if done == PENDING:
                    q.put((PENDING, "", None))
                elif not done:
                    q.put((FAILED, "", None))

            threading.Thread(target=_do, daemon=True, name="update-apply").start()

        cv.tag_bind("btn", "<Button-1>", lambda e: _on_button())
        cv.tag_bind("btn", "<Enter>", lambda e: (
            cv.itemconfigure(btn_bg, fill=_ACCENT), cv.configure(cursor="hand2")))
        cv.tag_bind("btn", "<Leave>", lambda e: (
            cv.itemconfigure(btn_bg, fill=_ACCENT_HOT), cv.configure(cursor="")))
        root.bind("<Escape>", lambda e: hide())
        root.bind("<Return>", lambda e: _on_button())
        apply_state(state, version, on_update)
        if _demo_autoclose_ms:
            root.after(_demo_autoclose_ms, hide)
        root.focus_force()
    except Exception as exc:
        log.warning("update-popup: окно не создано: %r", exc)
        try:
            if root is not None:
                _hide(root)
        except Exception:
            pass
        ready.set()
        return
    ok.append(True)
    ready.set()

    def _poll():
        try:
            while True:
                s, v, cb = q.get_nowait()
                apply_state(s, v, cb)
        except queue.Empty:
            pass
        root.after(100, _poll)

    root.after(100, _poll)
    try:
        root.mainloop()
    except Exception as exc:
        log.warning("update-popup: mainloop: %r", exc)
