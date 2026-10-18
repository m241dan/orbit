"""The repository's count of commits since the last super review, kept apart from any one
session."""

import asyncio

from orbit.agent.application.failures import outcome
from orbit.agent.application.ports import SessionStore
from orbit.agent.application.storage_halt import Halting
from orbit.agent.domain.storage_halt import FailedWrite


class ReviewCount:
    """The repository's super review count, saved in ``store`` (if there is one); a failing save
    halts the session through ``halt``, and nothing is saved while it's halted."""

    def __init__(self, store: SessionStore | None, halt: Halting) -> None:
        self._store = store
        self._halt = halt
        self._count: int | None = None  # The count the store holds.
        self._saving = asyncio.Lock()  # Saves one at a time, so the newest is written last.

    async def load(self) -> int | None:
        """The repository's commits since the last super review; none without a store."""
        if self._store is None:
            return None
        self._count = await self._store.review_count()
        return self._count

    async def save(self, count: int) -> None:
        """Save ``count`` as the repository's commits since the last super review, if it
        changed."""
        if self._store is None:
            return
        async with self._saving:
            if count == self._count or self._halt.halted:
                return
            error = await outcome(self._store.save_review_count(count))
            if isinstance(error, RuntimeError):
                self._halt.fail(FailedWrite.REVIEW_COUNT, error)
            else:
                self._count = count
