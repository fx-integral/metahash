# finance/mp_compat.py
# --------------------------------------------------------------------------- #
# Some environments (notably sandboxed CI) forbid POSIX semaphore creation,
# which breaks Python's `multiprocessing.Queue`.  Bittensor's logging stack
# spins up a queue during import, so we proactively replace it with a thin
# thread-based queue when the standard queue fails to initialise.
# --------------------------------------------------------------------------- #

from __future__ import annotations

import multiprocessing as _mp
from queue import Queue as _ThreadQueue
from typing import Any


def _safe_queue(maxsize: int = -1) -> _ThreadQueue:
    # queue.Queue treats maxsize <= 0 as infinite
    return _ThreadQueue(maxsize if maxsize and maxsize > 0 else 0)


def ensure_multiprocessing_compat() -> None:
    try:
        # Probe once; failure triggers the fallback.
        test_queue = _mp.Queue(-1)
        test_queue.close()
        test_queue.join_thread()
    except (PermissionError, OSError):
        class _CompatQueue(_ThreadQueue):
            def __init__(self, maxsize: int = -1):
                super().__init__(maxsize if maxsize and maxsize > 0 else 0)

            def close(self) -> None:  # type: ignore[override]
                pass

            def join_thread(self) -> None:  # type: ignore[override]
                pass

        _mp.Queue = _CompatQueue  # type: ignore[assignment]

