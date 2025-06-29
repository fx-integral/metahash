from __future__ import annotations

import asyncio
from functools import wraps
from typing import Any, Callable, Coroutine, TypeVar

T = TypeVar("T")

# ── core helper ────────────────────────────────────────────────────────── #


async def maybe_async(fn: Callable[..., T], *args, **kwargs) -> T:          # noqa: N802
    """
    Await *fn* whether it is a coroutine or a plain blocking function.
    Plain calls are off‑loaded to the default thread‑pool.
    """
    if asyncio.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)                                     # type: ignore[misc]
    return await asyncio.to_thread(fn, *args, **kwargs)                      # type: ignore[arg-type]


# ── optional decorator for sync entry‑points ───────────────────────────── #

def run_maybe_async(func: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., T]:
    """
    Turns an *async* function into one that can be called from sync code,
    running its coroutine in a fresh event‑loop when needed.

    >>> @run_maybe_async
    ... async def main():
    ...     # you can freely mix `maybe_async` substrate calls here
    ...     ...
    >>> main()          # works in __main__ or tests without `asyncio.run`
    """
    @wraps(func)
    def _wrapper(*args, **kwargs):  # type: ignore[override]
        coro = func(*args, **kwargs)
        if asyncio.get_event_loop().is_running():       # already inside an event‑loop
            return coro                                 # let caller `await` it
        return asyncio.run(coro)                        # run & return result synchronously

    return _wrapper
