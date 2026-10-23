"""Calls to the store that may fail, failures reported only once until a call works again, and
unexpected errors, whose tracebacks are logged."""

import logging
from collections.abc import Awaitable

from orbit.agent.application.ports import Publish, StoreFailed
from orbit.protocol import ErrorOccurred

logger = logging.getLogger(__name__)


def unexpected(message: str, exc: BaseException) -> str:
    """``message``, telling the user of ``exc``, which nothing expected; ``exc`` is logged with
    its traceback, as the message alone loses its cause."""
    logger.error("%s", message, exc_info=exc)
    return message


async def outcome[T](call: Awaitable[T]) -> T | StoreFailed:
    """What ``call``, to the store, returns, or the :class:`StoreFailed` it raises when it
    fails; any other error is raised."""
    try:
        return await call
    except StoreFailed as exc:
        return exc


class ReportedOnce:
    """Calls to the store of one kind, saves or reads, whose first failure since the last that
    worked is reported with ``publish`` as an error coded ``code``."""

    def __init__(self, publish: Publish, code: str) -> None:
        self._publish = publish
        self._code = code
        self._failing = False  # A call failed, and was reported, since the last that worked.

    async def attempt(self, call: Awaitable[None]) -> bool:
        """Await ``call``; returns whether it worked. A failure is reported unless one was since
        the last call that worked, and work carries on."""
        return not isinstance(await self._checked(call), StoreFailed)

    async def value[T](self, call: Awaitable[T]) -> T | None:
        """What ``call`` returns; none if it fails, reported unless a failure was since the last
        call that worked."""
        result = await self._checked(call)
        return None if isinstance(result, StoreFailed) else result

    async def _checked[T](self, call: Awaitable[T]) -> T | StoreFailed:
        """What ``call`` returns, or the :class:`StoreFailed` it raises, reported unless a
        failure was since the last call that worked."""
        result = await outcome(call)
        if not isinstance(result, StoreFailed):
            self._failing = False
            return result
        if not self._failing:
            self._failing = True
            await self._publish(ErrorOccurred(code=self._code, message=str(result)))
        return result
