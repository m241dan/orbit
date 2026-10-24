"""The offer of a freed repository: whether to take it over or stay read-only."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from orbit.protocol import Command, TakeOwnership
from orbit.tui.view.outcomes import Awaited

Send = Callable[[Command, Awaited], Awaitable[object]]
"""Sends a command to the backend, awaiting its outcome as told; whatever it returns is
ignored."""

Answered = Callable[[bool | None], Awaitable[None]]
"""Takes whether the user chose to take the repository over, if the modal was answered."""


class OfferView(Protocol):
    """Where the offer shows: its modal, one at a time, which the user answers."""

    @property
    def modal_open(self) -> bool:
        """Whether the offer's modal is open."""
        ...

    def open_modal(self, answered: Answered) -> None:
        """Open the modal asking whether to take the repository over, calling ``answered``
        with the user's choice once they answer it."""
        ...

    def close_modal(self) -> None:
        """Close the modal, if it's open, unanswered, calling back nothing."""
        ...


class TakeoverOffer:
    """Follows the offer of the repository: while it's open, asks about it in ``view``, one
    modal at a time, sending the user's choice with ``send`` and asking again if it's refused
    while the offer is still open; once the offer is settled, from here or another frontend,
    closes the modal still open."""

    def __init__(self, view: OfferView, send: Send) -> None:
        self._view = view
        self._send = send
        self.open = False  # Whether the offer is open, as far as this frontend knows.

    def offered(self) -> None:
        """Ask whether to take over the repository, unless the modal is open already."""
        self.open = True
        if not self._view.modal_open:
            self._view.open_modal(self._answered)

    async def _answered(self, take: bool | None) -> None:
        """Send the user's choice to ``take`` over the repository, or not."""
        await self._send(TakeOwnership(take=bool(take)), Awaited(refused=self._refused))

    def _refused(self) -> None:
        """Ask again, if the offer is still open after an answer was refused."""
        if self.open:
            self.offered()

    def settled(self) -> None:
        """Close the modal, unanswered, if it's open."""
        self.open = False
        self._view.close_modal()

    def follow(self, offered: bool) -> None:
        """Ask, if the repository is ``offered``, else close the modal."""
        if offered:
            self.offered()
        else:
            self.settled()
