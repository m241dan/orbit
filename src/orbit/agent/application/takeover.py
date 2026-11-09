"""Taking over a repository: a read-only orbit waits for the one owning it to go, then offers
the user to own it in its place."""

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum, auto

from orbit.agent.application.lanes import Background
from orbit.agent.application.ports import Publish, RepositoryLock
from orbit.agent.domain.refusals import Refused
from orbit.execution.sleeping import Sleep, sleep_for
from orbit.protocol import INVALID, OwnershipOffered

RETRY = timedelta(seconds=5)
"""How long a read-only orbit waits between tries of the repository's lock."""

DECLINED_RETRY = timedelta(minutes=5)
"""How long a read-only orbit waits after a declined offer before offering a repository that
has stayed free since."""

NO_OFFER = "No offer of the repository is open: another orbit still owns it."


@dataclass(frozen=True)
class RetryTimes:
    """How long to wait between tries of the lock, ``interval``, and after a declined offer
    before offering a repository that has stayed free since, ``declined``."""

    interval: timedelta = RETRY
    declined: timedelta = DECLINED_RETRY


DEFAULT_TIMES = RetryTimes()
"""The waits a Takeover uses unless told otherwise."""


class Takeover:
    """Whether this orbit is read-only, as another orbit owns the repository, until it takes
    the repository over. While ``read_only``, it tries ``lock`` every ``times.interval``,
    waited out with ``sleep``, once started; once it's taken, holds it and publishes
    ``OwnershipOffered`` with ``publish``, once, until the offer is answered. After a decline, it
    offers again only once another orbit has held the lock since, or after ``times.declined``.
    Without a ``lock``, or once owning, it never offers anything."""

    def __init__(
        self,
        read_only: bool,
        lock: RepositoryLock | None,
        publish: Publish,
        times: RetryTimes = DEFAULT_TIMES,
        sleep: Sleep = sleep_for,
    ) -> None:
        self._read_only = read_only
        self._lock = lock if read_only else None
        self._publish = publish
        self._times = times
        self._sleep = sleep
        self._background = Background("ownership", publish)
        self._offered = False

    @property
    def read_only(self) -> bool:
        """Whether another orbit owns the repository, so work is queued and reviews wait."""
        return self._read_only

    @property
    def offered(self) -> bool:
        """Whether an offer is open, waiting for its answer."""
        return self._offered

    def start(self) -> None:
        """Start trying the lock in the background, if there's one."""
        if self._lock is not None:
            self._background.start(self._retry())

    def answer(self, take: bool) -> None:
        """Settle the offer: keep the lock and own the repository if ``take``, else let it go
        and offer it again once another orbit has held it or the back-off has passed; refused
        with ``invalid`` when no offer is open."""
        if not self._offered:
            raise Refused(INVALID, NO_OFFER)
        self._offered = False
        if take:
            self._read_only = False
            return
        assert self._lock is not None  # Only a lock taken is offered.
        self._lock.close()
        self._background.start(self._retry(self._times.declined))

    async def close(self) -> None:
        """Stop trying the lock."""
        await self._background.close()

    async def _retry(self, hold_off: timedelta = timedelta()) -> None:
        """Try the lock every interval until it's taken, then offer the repository; until the
        intervals waited add up to ``hold_off``, a lock taken is let go again unless another
        orbit has held it since."""
        assert self._lock is not None  # Only started with a lock.
        interval = self._times.interval
        left = hold_off
        while True:
            await self._sleep(interval)
            left -= interval
            attempt = _attempt(self._lock)
            if attempt is _Attempt.HELD:
                left = timedelta()
            elif attempt is _Attempt.TAKEN:
                if left <= timedelta():
                    break
                # Still free since the decline: keep watching without holding it.
                self._lock.close()
        await self._publish(OwnershipOffered())
        # Open once published, so an answer finds this try ended and can start another.
        self._offered = True


class _Attempt(Enum):
    """How a try of the lock went."""

    TAKEN = auto()
    HELD = auto()
    """Another orbit holds it."""
    FAILED = auto()
    """It couldn't be made; it's tried again later."""


def _attempt(lock: RepositoryLock) -> _Attempt:
    """How taking ``lock`` now went."""
    try:
        owner = lock.take()
    except OSError:
        return _Attempt.FAILED
    return _Attempt.TAKEN if owner is None else _Attempt.HELD
