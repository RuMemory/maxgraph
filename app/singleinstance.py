#!/usr/bin/env python3
"""Single-instance guard: один живой процесс на один data/ (локфайл <data>/.lock).

Windows — msvcrt.locking (LK_NBLCK) на открытом дескрипторе; Linux/macOS —
fcntl.flock(LOCK_EX|LOCK_NB). Дескриптор держим открытым до смерти процесса
(ОС снимает блокировку сама, даже при crash), файл не удаляем. Блокировка
привязана к data_dir: установки/spike-экземпляры с разными MEMORY_DIR живут
параллельно.

Fail-open: data/ недоступен (OSError на открытии) или ФС без поддержки
блокировок → True, старое поведение (эфемерный токен) не ломаем.
Escape hatch: MG_NO_LOCK=1 → всегда True (тесты, spike).
"""
from __future__ import annotations

import os
import sys
import time

_lock_fd = None  # дескриптор .lock — открыт до конца процесса (иначе блокировка слетит)

# errno гонки блокировки: EACCES/EDEADLK (Windows msvcrt) + EAGAIN/EWOULDBLOCK (flock).
# PermissionError/BlockingIOError покрывают их на уровне классов; числа — страховка.
_CONTENTION_ERRNOS = {11, 13, 36}


def acquire(data_dir, timeout: float = 3.0) -> bool:
    """Эксклюзивная блокировка <data_dir>/.lock на время жизни процесса.

    True — мы единственный экземпляр (или guard выключен/недоступен);
    False — этот data/ уже держит другой живой процесс.
    timeout — окно ретраев при гонке старта (Windows os.execv из трея:
    ребёнок стартует до полной смерти родителя, блокировка ещё не снята ОС).
    """
    global _lock_fd
    if os.environ.get("MG_NO_LOCK") == "1":
        return True
    try:
        os.makedirs(data_dir, exist_ok=True)
        fd = os.open(os.path.join(os.fspath(data_dir), ".lock"), os.O_RDWR | os.O_CREAT)
    except OSError:
        return True  # data/ недоступен — fail-open, как эфемерный токен
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
            if not held:  # ФС без блокировок и прочие чудачества — fail-open
                os.close(fd)
                return True
            if time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(0.1)
