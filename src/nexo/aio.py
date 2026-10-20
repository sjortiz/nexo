"""Run a coroutine from synchronous code, whether or not an event loop is already running."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")


def run_sync(coroutine: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run`, also from inside a running loop (Jupyter, an async web server).

    There it runs on a worker thread with a copy of the caller's context, so the
    active OpenTelemetry span stays the parent of whatever the coroutine traces.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    context = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="nexo-run-sync") as pool:
        return pool.submit(context.run, asyncio.run, coroutine).result()
