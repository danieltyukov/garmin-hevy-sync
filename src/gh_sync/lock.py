"""One sync at a time.

A manual ``sync`` started while the scheduler's run is in flight, or a Docker
loop and a host timer pointed at the same home, would both pass flow B's
ledger check for the same activity before either recorded it, and import it
twice. An OS-level file lock is released automatically if the process dies,
so there is no stale lock to clean up after a crash.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")  # noqa: SIM115 - held open for the lock's lifetime
    try:
        try:
            _acquire(handle)
        except OSError:
            raise AlreadyRunning(
                f"Another sync is already running (lock held on {path})."
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            _release(handle)
    finally:
        handle.close()


if os.name == "nt":
    import msvcrt

    def _acquire(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _release(handle) -> None:
        handle.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _acquire(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
