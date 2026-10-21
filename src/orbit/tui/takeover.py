"""The modal offering a freed repository: take it over or stay read-only."""

from collections.abc import Awaitable, Callable

from orbit.tui.modal import Closed, ModalSlot
from orbit.tui.view.confirm import TAKE_OVER_MESSAGE, TAKE_OVER_NO, TAKE_OVER_TITLE, TAKE_OVER_YES
from orbit.tui.view.outcomes import Awaited
from orbit.tui.widgets import ConfirmScreen

Answer = Callable[[bool, Awaited], Awaitable[object]]
Push = Callable[[ConfirmScreen, Closed[bool]], object]


class TakeoverHost:
    """Follows the offer of the repository: while it's open, asks about it in a modal, one at a
    time, with ``push``, sending the user's choice with ``answer`` and asking again if it's
    refused while the offer is still open; once the offer is settled, from here or another
    frontend, closes the modal still open."""

    def __init__(self, push: Push, answer: Answer) -> None:
        self._slot = ModalSlot[ConfirmScreen, bool](push)
        self._answer = answer
        self.open = False  # Whether the offer is open, as far as this frontend knows.

    @property
    def shown(self) -> ConfirmScreen | None:
        """The modal open, if any."""
        return self._slot.showing

    def offered(self) -> None:
        """Ask whether to take over the repository, unless the modal is open already."""
        self.open = True
        if self.shown is not None:
            return
        screen = ConfirmScreen(TAKE_OVER_TITLE, TAKE_OVER_MESSAGE, TAKE_OVER_YES, TAKE_OVER_NO)
        self._slot.open(screen, self._answered)

    async def _answered(self, take: bool | None) -> None:
        """Send the user's choice to ``take`` over the repository, or not."""
        await self._answer(bool(take), Awaited(refused=self._refused))

    def _refused(self) -> None:
        """Ask again, if the offer is still open after an answer was refused."""
        if self.open:
            self.offered()

    def settled(self) -> None:
        """Close the modal, unanswered, if it's open."""
        self.open = False
        self._slot.close(None)

    def follow(self, offered: bool) -> None:
        """Ask, if the repository is ``offered``, else close the modal."""
        if offered:
            self.offered()
        else:
            self.settled()
