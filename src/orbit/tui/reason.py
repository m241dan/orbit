"""The modal that says why a task ended."""

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from orbit.protocol import TaskState
from orbit.tui.view.detail import copied_note
from orbit.tui.view.tasks import REASON_HINT, reason_text, reason_title


class ReasonScreen(ModalScreen[None]):
    """Why task ``number`` ended, in full, as ``state`` had it when opened.

    j/k and the arrow keys scroll, y copies the text and escape closes.
    """

    AUTO_FOCUS = None
    DEFAULT_CSS = """
    ReasonScreen { align: center middle; }
    ReasonScreen > VerticalScroll {
        width: 100;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: round $accent;
        padding: 0 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j,down", "scroll(1)", show=False),
        Binding("k,up", "scroll(-1)", show=False),
        Binding("y", "copy", show=False),
        Binding("escape", "close", show=False),
    ]

    def __init__(self, state: TaskState, number: str) -> None:
        super().__init__()
        self.title_text = reason_title(state, number)
        self.text = reason_text(state)

    def compose(self) -> ComposeResult:
        with VerticalScroll(can_focus=False) as box:
            box.border_title = self.title_text
            box.border_subtitle = REASON_HINT
            yield Static(Text(self.text), id="reason-body")

    def action_scroll(self, step: int) -> None:
        self.query_one(VerticalScroll).scroll_relative(y=step, animate=False)

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self.text)
        self.app.notify(copied_note(self.text))

    def action_close(self) -> None:
        self.dismiss(None)
