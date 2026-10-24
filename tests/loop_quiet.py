"""Telling when the event loop has gone quiet, so a test waiting for a backend in this process to
stop sending stops as soon as nothing more can come, rather than after a fixed while."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

_CALM_TURNS = 3
"""How many turns of the loop in a row must find nothing else to run."""


async def quiet_within(window: float) -> bool:
    """Wait until nothing more can happen in the event loop, or ``window`` seconds at the most;
    returns whether it went quiet. Quiet is nothing else ready to run, no timer due within
    ``window``, no thread of the loop's at work, and no file watched but the loop's own wake-up
    pipe; a timer due within ``window`` is waited for. Where it can't tell, as when a socket,
    a pipe or another thread is about, it waits the whole ``window``, as a test did before."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + window
    calm = 0
    while calm < _CALM_TURNS:
        left = deadline - loop.time()
        due = _next_due(loop, window)
        if left <= 0:
            return False
        if due is None:
            await asyncio.sleep(left)
            return False
        if due > 0:
            await asyncio.sleep(min(due, left))
            calm = 0
            continue
        await asyncio.sleep(0)
        calm = 0 if _ready(loop) else calm + 1
    return True


def _ready(loop: asyncio.AbstractEventLoop) -> bool:
    """Whether anything else is ready to run in ``loop``."""
    return bool(getattr(loop, "_ready", True))


def _next_due(loop: asyncio.AbstractEventLoop, window: float) -> float | None:
    """How long until the next of ``loop``'s timers due within ``window``, 0 for none; none when
    the loop can't be told quiet: it watches more than its wake-up pipe, a thread of its
    executor is at work, another thread is about, or its insides aren't the ones known."""
    try:
        watched = len(loop._selector.get_map())  # type: ignore[attr-defined]
        scheduled = loop._scheduled  # type: ignore[attr-defined]
        executor = loop._default_executor  # type: ignore[attr-defined]
    except AttributeError:
        return None
    if watched != 1 or not _threads_idle(executor):
        return None
    return _soonest(loop, scheduled, window)


def _soonest(
    loop: asyncio.AbstractEventLoop, scheduled: list[asyncio.TimerHandle], window: float
) -> float:
    """How long until the soonest of the timers ``scheduled`` in ``loop`` that's due within
    ``window``, a moment for one overdue; 0 for none."""
    now = loop.time()
    due = [timer.when() - now for timer in scheduled if not timer.cancelled()]
    soonest = min(due, default=window + 1)
    return max(soonest, 1e-6) if soonest <= window else 0.0


def _threads_idle(executor: ThreadPoolExecutor | None) -> bool:
    """Whether no thread but the main one and ``executor``'s idle ones is about."""
    if executor is None:
        return threading.active_count() == 1
    try:
        workers = len(executor._threads)
        idle = executor._idle_semaphore._value
        queued = executor._work_queue.qsize()
    except AttributeError:
        return False
    return workers == idle and queued == 0 and threading.active_count() == 1 + workers
