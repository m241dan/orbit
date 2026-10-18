"""Calls to the store that may fail, and failures reported only once until a call works again."""

from collections.abc import Awaitable

from orbit.agent.application.ports import Publish
from orbit.protocol import SAVE_FAILED, ErrorOccurred


async def outcome[T](call: Awaitable[T]) -> T | RuntimeError:
    """What ``call``, to the store, returns, or the ``RuntimeError`` it raises when it fails."""
    try:
        return await call
    except RuntimeError as exc:
        return exc


class ReportedOnce:
    """Saves of one kind whose first failure since the last that worked is reported with
    ``publish``."""

    def __init__(self, publish: Publish) -> None:
        self._publish = publish
        self._failing = False  # A save failed, and was reported, since the last that worked.

    def forget(self) -> None:
        """Report the next failure, as though the last save worked."""
        self._failing = False

    async def attempt(self, saving: Awaitable[None]) -> bool:
        """Await ``saving``; returns whether it worked. A failure is reported unless one was
        since the last save that worked, and work carries on."""
        error = await outcome(saving)
        if not isinstance(error, RuntimeError):
            self._failing = False
            return True
        if not self._failing:
            self._failing = True
            await self._publish(ErrorOccurred(code=SAVE_FAILED, message=str(error)))
        return False
