# metahash/utils/async_debug.py
import asyncio
import threading
import traceback
import time


def loop_info_dict(prefix: str = "") -> dict:
    """Get comprehensive information about the current event loop and thread context."""
    d = {
        "thread": threading.current_thread().name,
        "thread_ident": threading.get_ident(),
        "has_running_loop": False,
        "loop_id": None,
        "loop_is_running": None,
        "ts": time.time(),
        "prefix": prefix,
    }
    try:
        loop = asyncio.get_running_loop()
        d["has_running_loop"] = True
        d["loop_id"] = id(loop)
        d["loop_is_running"] = loop.is_running()
    except RuntimeError:
        pass
    return d


def short_tb(limit: int = 5) -> str:
    """Get a short stack trace for debugging call sites."""
    return "".join(traceback.format_stack(limit=limit))  # no exception, just current stack
