"""run_sync: synchronous entry points must work in scripts and inside running loops (notebooks)."""

from __future__ import annotations

import asyncio
import contextvars
import threading

import pytest

from nexo.aio import run_sync

REQUEST = contextvars.ContextVar("request", default="unset")


async def _where() -> tuple[str, str]:
    await asyncio.sleep(0)
    return REQUEST.get(), threading.current_thread().name


def test_without_a_running_loop_it_is_asyncio_run():
    """Without a running loop the coroutine runs on the calling thread and context."""
    REQUEST.set("script")
    value, thread = run_sync(_where())
    assert value == "script" and thread == threading.current_thread().name


def test_inside_a_running_loop_it_runs_on_a_worker_with_the_callers_context():
    """Inside a running loop it runs on a worker thread that sees the caller's context."""

    async def notebook_cell():
        REQUEST.set("cell")
        with pytest.raises(
            RuntimeError, match="running event loop"
        ):  # why run_sync exists
            coroutine = _where()
            try:
                asyncio.run(coroutine)
            finally:
                # pylint infers the async def's return type, not the coroutine object.
                coroutine.close()  # pylint: disable=no-member
        return run_sync(_where())

    value, thread = asyncio.run(notebook_cell())
    assert value == "cell"
    assert thread.startswith("nexo-run-sync")


def test_errors_propagate_from_the_worker():
    """An exception raised in the worker coroutine reaches the caller."""

    async def fails():
        raise ValueError("boom")

    async def notebook_cell():
        return run_sync(fails())

    with pytest.raises(ValueError, match="boom"):
        asyncio.run(notebook_cell())
