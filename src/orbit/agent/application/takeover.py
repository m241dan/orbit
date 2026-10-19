"""Taking over a repository: a read-only orbit waits for the one owning it to go, then offers
the user to own it in its place."""

import asyncio

from orbit.agent.application.lanes import Background
from orbit.agent.application.ports import Publish, RepositoryLock
from orbit.agent.domain.refusals import Refused
from orbit.protocol import INVALID, OwnershipOffered

RETRY_SECONDS = 5.0
"""How long a read-only orbit waits between tries of the repository's lock."""

NO_OFFER = "No offer of the repository is open: another orbit still owns it."


class Takeover:
    """Tries ``lock`` every ``interval`` seconds, once started; once it's taken, holds it and
    publishes ``OwnershipOffered`` with ``publish``, once, until the offer is answered. Without
    a ``lock`` it never offers anything."""

    def __init__(
        self, lock: RepositoryLock | None, publish: Publish, interval: float = RETRY_SECONDS
    ) -> None:
        self._lock = lock
        self._publish = publish
        self._interval = interval
        self._background = Background("ownership", publish)
        self._offered = False

    @property
    def open(self) -> bool:
        """Whether an offer is open, waiting for its answer."""
        return self._offered

    def start(self) -> None:
        """Start trying the lock in the background, if there's one."""
        if self._lock is not None:
            self._background.start(self._retry())

    def answer(self, take: bool) -> None:
        """Settle the offer: keep the lock if ``take``, else let it go and try again later;
        refused with ``invalid`` when no offer is open."""
        if not self._offered:
            raise Refused(INVALID, NO_OFFER)
        self._offered = False
        if not take:
            assert self._lock is not None  # Only a lock taken is offered.
            self._lock.close()
            self.start()

    async def close(self) -> None:
        """Stop trying the lock."""
        await self._background.close()

    async def _retry(self) -> None:
        """Try the lock every interval until it's taken, then offer the repository."""
        assert self._lock is not None  # Only started with a lock.
        while True:
            await asyncio.sleep(self._interval)
            if _took(self._lock):
                break
        await self._publish(OwnershipOffered())
        # Open once published, so an answer finds this try ended and can start another.
        self._offered = True


def _took(lock: RepositoryLock) -> bool:
    """Whether ``lock`` was taken now; a lock that can't be made is tried again later."""
    try:
        return lock.take() is None
    except OSError:
        return False
