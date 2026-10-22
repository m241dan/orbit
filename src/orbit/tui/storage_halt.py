"""The modal and status line that show a session halted because storage failed."""

from collections.abc import Awaitable, Callable
from typing import ClassVar

from textual.binding import Binding, BindingType

from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.storage import HALT_HINT, HALT_TITLE, halt_status
from orbit.tui.widgets import TextScreen

Retry = Callable[[], Awaitable[object]]


class StorageHaltScreen(TextScreen):
    """Why the session halted, in full, from ``explanation``: enter calls ``retry`` to try
    again, staying open until the halt lifts; j/k and the arrow keys scroll and escape closes."""

    DEFAULT_CSS = """
    StorageHaltScreen > VerticalScroll { border: round $error; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("enter", "try_again", show=False)]

    def __init__(self, explanation: str, retry: Retry) -> None:
        super().__init__(HALT_TITLE, HALT_HINT, explanation)
        self._retry = retry

    async def action_try_again(self) -> None:
        await self._retry()


ShowStatus = Callable[[str | None], None]


class StorageHaltHost:
    """Follows whether the session is halted: while it is, the status line is shown with
    ``show_status`` and the halt's modal, one at a time, with ``push``; ``retry`` tries again."""

    def __init__(
        self, push: Push[StorageHaltScreen, None], retry: Retry, show_status: ShowStatus
    ) -> None:
        self._slot = ModalSlot[StorageHaltScreen, None](push)
        self._retry = retry
        self._show_status = show_status
        self.explanation: str | None = None  # Why the session is halted; none while it isn't.

    @property
    def shown(self) -> StorageHaltScreen | None:
        """The modal open, if any."""
        return self._slot.showing

    def halted(self, explanation: str) -> None:
        """Show that the session is halted for ``explanation``, opening the modal or showing it
        there if it's open."""
        self.explanation = explanation
        self._show_status(halt_status(explanation))
        if self.shown is not None:
            self.shown.show(explanation)
            return
        self._slot.open(StorageHaltScreen(explanation, self._retry), _nothing)

    def lifted(self) -> None:
        """Show that the session isn't halted, closing the modal if it's open."""
        self.explanation = None
        self._show_status(None)
        self._slot.close(None)


async def _nothing(_: None) -> None:
    """Do nothing once the modal is closed."""
