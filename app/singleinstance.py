#!/usr/bin/env python3












from __future__ import annotations

import os
import sys
import time

_lock_fd = None



_CONTENTION_ERRNOS = {11, 13, 36}


def acquire(data_dir, timeout: float = 3.0) -> bool:







    global _lock_fd
    if os.environ.get("MG_NO_LOCK") == "1":
        return True
    try:
        os.makedirs(data_dir, exist_ok=True)
        fd = os.open(os.path.join(os.fspath(data_dir), ".lock"), os.O_RDWR | os.O_CREAT)
    except OSError:
        return True
    deadline = time.monotonic() + timeout
    while True:
        try:
            if sys.platform.startswith("win"):
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _lock_fd = fd
            return True
        except OSError as exc:
            held = isinstance(exc, (PermissionError, BlockingIOError)) \
                or exc.errno in _CONTENTION_ERRNOS
            if not held:
                os.close(fd)
                return True
            if time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(0.1)
