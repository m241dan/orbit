"""How many coders run at once across the workspaces a control plane serves: each task of a role
that counts takes a slot as its agent starts and gives it back once it ends, and while every
slot is taken it waits in line, behind those that asked before it."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass


def _unwatched() -> None:
    """Note a change no one watches."""


@dataclass(eq=False)
class _Place:
    """A place in line, of the ``workspace``'s ``session``, ``granted`` a slot (true) or dropped
    (false)."""

    workspace: str
    session: str
    granted: "asyncio.Future[bool]"

    def at(self, workspace: str, session: str | None) -> bool:
        """Whether this is the ``workspace``'s ``session``'s place, or any of its sessions' if
        none is given."""
        return self.workspace == workspace and session in (None, self.session)


class CoderSlots:
    """``limit`` slots for coders, or as many as asked for if it's 0, taken first come, first
    served: one asked for while every slot is taken, or while others wait, waits in line behind
    them. ``changed`` is called whenever the line changes, so positions in it may have."""

    def __init__(self, limit: int = 0, changed: Callable[[], None] = _unwatched) -> None:
        self._limit = limit
        self.changed = changed
        self._taken = 0
        self._line: list[_Place] = []

    @property
    def taken(self) -> int:
        """How many slots are taken."""
        return self._taken

    def cap(self, limit: int) -> None:
        """Have ``limit`` slots from now on, or as many as asked for if it's 0, granting those
        waiting any that are free."""
        self._limit = limit
        self._grant()

    def position(self, workspace: str, session: str) -> int | None:
        """Where the ``workspace``'s ``session`` waits in line, 1 next; none if it doesn't."""
        places = enumerate(self._line, 1)
        return next((n for n, place in places if place.at(workspace, session)), None)

    async def acquire(self, workspace: str, session: str) -> bool:
        """Take a slot for the ``workspace``'s ``session``, waiting in line for one; returns
        whether it took one, false if its place was dropped. Cancelled, it leaves the line, or
        gives back the slot it was just granted."""
        if not self._line and self._free:
            self._taken += 1
            return True
        place = _Place(workspace, session, asyncio.get_running_loop().create_future())
        self._line.append(place)
        self.changed()
        try:
            return await place.granted
        except asyncio.CancelledError:
            self._left(place)
            raise

    def release(self) -> None:
        """Give back a slot taken, granting it to the next in line, if any."""
        self._taken -= 1
        self._grant()

    def drop(self, workspace: str, session: str | None = None) -> None:
        """Drop the places in line of the ``workspace``'s ``session``, or of its every one if
        none is given, so they take no slot."""
        dropped = [place for place in self._line if place.at(workspace, session)]
        for place in dropped:
            self._line.remove(place)
            place.granted.set_result(False)
        if dropped:
            self.changed()

    @property
    def _free(self) -> bool:
        return self._limit == 0 or self._taken < self._limit

    def _grant(self) -> None:
        """Grant the free slots to those first in line."""
        granted = False
        while self._line and self._free:
            self._line.pop(0).granted.set_result(True)
            self._taken += 1
            granted = True
        if granted:
            self.changed()

    def _left(self, place: _Place) -> None:
        """``place``'s wait was cancelled: it leaves the line, or, granted a slot meanwhile,
        gives it back."""
        if place in self._line:
            self._line.remove(place)
            self.changed()
        elif place.granted.result():
            self.release()


@dataclass(frozen=True)
class CoderSeat:
    """A work lane's seat at the coder slots ``slots`` gives, for the workspace ``workspace``'s
    session ``session`` says is open."""

    slots: Callable[[], CoderSlots]
    workspace: str
    session: Callable[[], str]

    async def acquire(self) -> bool:
        """Take a slot, as :meth:`CoderSlots.acquire` does."""
        return await self.slots().acquire(self.workspace, self.session())

    def release(self) -> None:
        """Give back a slot taken."""
        self.slots().release()

    def drop(self) -> None:
        """Drop the open session's place in line, if it waits in it."""
        self.slots().drop(self.workspace, self.session())

    @property
    def busy(self) -> int:
        """How many coders' slots are taken."""
        return self.slots().taken

    @property
    def position(self) -> int | None:
        """Where the open session waits in line, 1 next; none if it doesn't."""
        return self.slots().position(self.workspace, self.session())
