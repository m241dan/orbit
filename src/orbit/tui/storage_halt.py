"""The modal and status line that show a session halted because storage failed."""

from collections.abc import Awaitable, Callable
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.storage import HALT_HINT, HALT_TITLE, halt_status

Retry = Callable[[], Awaitable[object]]


class StorageHaltScreen(ModalScreen[None]):
    """Why the session halted, in full, from ``explanation``: enter calls ``retry`` to try
    again, staying open until the halt lifts, and escape closes it."""

    AUTO_FOCUS = None
    DEFAULT_CSS = """
    StorageHaltScreen { align: center middle; }
    StorageHaltScreen > VerticalScroll {
        width: 100;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: round $error;
        padding: 0 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "try_again", show=False),
        Binding("escape", "close", show=False),
    ]

    def __init__(self, explanation: str, retry: Retry) -> None:
        super().__init__()
        self.explanation = explanation
        self._retry = retry

    def compose(self) -> ComposeResult:
        with VerticalScroll(can_focus=False) as box:
            box.border_title = HALT_TITLE
            box.border_subtitle = HALT_HINT
            yield Static(Text(self.explanation), id="storage-halt-body")

    def show(self, explanation: str) -> None:
        """Show ``explanation`` in place of the one shown, or to be shown once composed."""
        self.explanation = explanation
        for body in self.query("#storage-halt-body").results(Static):
            body.update(Text(explanation))

    async def action_try_again(self) -> None:
        await self._retry()

    def action_close(self) -> None:
        self.dismiss(None)


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
