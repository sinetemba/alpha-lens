"""Cross-process job lock shared by the updater and the dashboard.

Only one process at a time may run a fetch/write job (price+news,
historical, IPO scan, cleanup, dashboard refresh) - two at once double-spend
API quota and race on dedupe. The lock is taken per JOB, not for the life of
the process: the always-on daemon must not lock out the logon/unlock refresh
or a manual `run_updater.py --full` for the whole session.

Within one process the lock is re-entrant and shared across threads (the
daemon's own threading locks already serialise same-type jobs; this guards
against *other* processes). It is per-machine: it can't stop a daemon on
another host sharing one Postgres.
"""
import os
import threading
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
JOB_LOCK_FILE = ROOT / "data" / ".updater.lock"


def acquire_instance_lock(path: Path):
    """Try once to take an exclusive OS file lock on `path`.

    Returns the open file object (keep it referenced; pass it to
    `release_instance_lock`) or None when another process holds it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def release_instance_lock(fh) -> None:
    """Release a lock taken by `acquire_instance_lock` and close the file."""
    if fh is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        fh.close()


class InterProcessJobLock:
    """Re-entrant (per process) wrapper around the OS file lock."""

    def __init__(self, path: Path = JOB_LOCK_FILE, poll_seconds: float = 2.0):
        self.path = Path(path)
        self.poll_seconds = poll_seconds
        self._mutex = threading.Lock()
        self._depth = 0
        self._fh = None

    def acquire(self, timeout: float = 0.0) -> bool:
        """Take the lock, waiting up to `timeout` seconds for another process."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._mutex:
                if self._depth:
                    self._depth += 1
                    return True
                fh = acquire_instance_lock(self.path)
                if fh is not None:
                    self._fh = fh
                    self._depth = 1
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(self.poll_seconds, remaining))

    def release(self) -> None:
        with self._mutex:
            if self._depth == 0:
                return
            self._depth -= 1
            if self._depth == 0:
                release_instance_lock(self._fh)
                self._fh = None

    @property
    def held(self) -> bool:
        return self._depth > 0


# The process-wide instance. Tests point it at a temp file (conftest).
JOB_LOCK: Optional[InterProcessJobLock] = InterProcessJobLock()
