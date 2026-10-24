"""The modal and status line that show a session halted because storage failed."""

from collections.abc import Awaitable, Callable
from typing import ClassVar

from textual.binding import Binding, BindingType

from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.outcomes import Send
from orbit.tui.view.storage import HALT_HINT, HALT_TITLE, StorageHalt
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
    """Shows the :class:`StorageHalt` it holds as ``halt``, which tries again with ``send``:
    its status line with ``show_status`` and its modal, one at a time, with ``push``."""

    def __init__(
        self, push: Push[StorageHaltScreen, None], send: Send, show_status: ShowStatus
    ) -> None:
        self._slot = ModalSlot[StorageHaltScreen, None](push)
        self._show_status = show_status
        self.halt = StorageHalt(self, send)

    @property
    def modal_open(self) -> bool:
        return self._slot.showing is not None

    def show_status(self, status: str | None) -> None:
        self._show_status(status)

    def open_modal(self, explanation: str) -> None:
        self._slot.open(StorageHaltScreen(explanation, self.halt.retry), _nothing)

    def show_in_modal(self, explanation: str) -> None:
        screen = self._slot.showing
        assert screen is not None  # The halt only shows in its modal while it's open.
        screen.show(explanation)

    def close_modal(self) -> None:
        self._slot.close(None)


async def _nothing(_: None) -> None:
    """Do nothing once the modal is closed."""
