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
        self._wanted: int | None = None  # The count last asked to be saved.
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
        self._wanted = count
        async with self._saving:
            if self._halt.halted:
                return
            error = await self._write(self._store, count)
            if error is not None:
                self._halt.fail(FailedWrite.REVIEW_COUNT, error)

    async def retry(self) -> RuntimeError | None:
        """Save the count last asked to be saved, if it changed, though the session is halted,
        as resuming does; returns the error when the save fails."""
        if self._store is None or self._wanted is None:
            return None
        async with self._saving:
            return await self._write(self._store, self._wanted)

    async def _write(self, store: SessionStore, count: int) -> RuntimeError | None:
        """Save ``count`` in ``store``, if it changed; returns the error when it fails."""
        if count == self._count:
            return None
        error = await outcome(store.save_review_count(count))
        if isinstance(error, RuntimeError):
            return error
        self._count = count
        return None
