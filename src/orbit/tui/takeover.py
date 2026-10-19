"""The modal offering a freed repository: take it over or stay read-only."""

from collections.abc import Awaitable, Callable

from orbit.tui.view.confirm import TAKE_OVER_MESSAGE, TAKE_OVER_NO, TAKE_OVER_TITLE, TAKE_OVER_YES
from orbit.tui.widgets import ConfirmScreen

Answer = Callable[[bool], Awaitable[object]]
Push = Callable[[ConfirmScreen, Callable[[bool | None], Awaitable[None]]], object]


class TakeoverHost:
    """Follows the offer of the repository: while it's open, asks about it in a modal, one at a
    time, with ``push``, sending the user's choice with ``answer``; once the offer is settled,
    from here or another frontend, closes the modal still open."""

    def __init__(self, push: Push, answer: Answer) -> None:
        self._push = push
        self._answer = answer
        self.shown: ConfirmScreen | None = None  # The modal open, if any.

    def offered(self) -> None:
        """Ask whether to take over the repository, unless the modal is open already."""
        if self.shown is not None:
            return
        screen = self.shown = ConfirmScreen(
            TAKE_OVER_TITLE, TAKE_OVER_MESSAGE, TAKE_OVER_YES, TAKE_OVER_NO
        )

        async def answered(take: bool | None) -> None:
            if self.shown is not screen:
                return  # Settled elsewhere, so there's nothing to answer.
            self.shown = None
            await self._answer(bool(take))

        self._push(screen, answered)

    def settled(self) -> None:
        """Close the modal, unanswered, if it's open."""
        screen = self.shown
        if screen is not None:
            self.shown = None
            screen.dismiss(None)

    def follow(self, offered: bool) -> None:
        """Ask, if the repository is ``offered``, else close the modal."""
        if offered:
            self.offered()
        else:
            self.settled()
