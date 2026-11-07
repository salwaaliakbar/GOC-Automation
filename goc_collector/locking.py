"""Cross-process lock so only one collection runs per database at a time.

Uses an OS-level file lock, which the operating system releases automatically
if the process exits or crashes (no stale lock files to clean up).
"""

from __future__ import annotations

import os
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class CollectionLocked(RuntimeError):
    pass


class CollectionLock:
    def __init__(self, db_path: str | Path):
        self.path = Path(str(db_path) + ".lock")
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise CollectionLocked(
                "Another collection is already running on this database "
                "(in another dashboard window or on the command line).")
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def is_locked_elsewhere(self) -> bool:
        if self.held:
            return False
        try:
            self.acquire()
        except CollectionLocked:
            return True
        self.release()
        return False
