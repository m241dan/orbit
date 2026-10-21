"""Closing every part in turn, so that a part failing, or the close being cancelled, leaves none
of the parts after it open."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence


async def close_all(stops: Sequence[Callable[[], Awaitable[None]]]) -> None:
    """Await each of ``stops`` in order, every one even after one before it fails or the close is
    cancelled; then re-raise the cancellation, if there was one, or else the first failure."""
    raised: list[BaseException] = []
    for stop in stops:
        exc = await _stopped(stop)
        if exc is not None:
            raised.append(exc)
    first = _first(raised)
    if first is not None:
        raise first


async def _stopped(stop: Callable[[], Awaitable[None]]) -> BaseException | None:
    """What awaiting ``stop`` raised, if it was cancelled or failed."""
    try:
        await stop()
    except asyncio.CancelledError as exc:  # Re-raised once the rest are stopped.
        return exc
    except Exception as exc:  # A bug; the rest must still stop.
        return exc
    return None


def _first(raised: list[BaseException]) -> BaseException | None:
    """Of ``raised``, the first cancellation, if any, or else the first failure."""
    cancelled = (exc for exc in raised if isinstance(exc, asyncio.CancelledError))
    return next(cancelled, None) or next(iter(raised), None)
