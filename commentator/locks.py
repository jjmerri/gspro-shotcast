"""A lock on a file that works across processes, on Windows and elsewhere."""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def file_lock(path: str | Path, timeout: float, busy: str = "held"):
    """Hold an exclusive lock on `path` (created if missing) until the block ends. Waits up to `timeout`
    seconds for another holder, then raises TimeoutError("<busy> <path> for <timeout>s")."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with open(path, "a+b") as f:
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{busy} {path} for {timeout:.0f}s") from None
                time.sleep(0.5)
        yield   # closing the file releases the lock
