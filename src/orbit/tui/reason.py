"""The modal that says why a task ended."""

from typing import ClassVar

from textual.binding import Binding, BindingType

from orbit.protocol import TaskState
from orbit.tui.view.detail import copied_note
from orbit.tui.view.tasks import REASON_HINT, reason_text, reason_title
from orbit.tui.widgets import TextScreen


class ReasonScreen(TextScreen):
    """Why task ``number`` ended, in full, as ``state`` had it when opened.

    j/k and the arrow keys scroll, y copies the text and escape closes.
    """

    BINDINGS: ClassVar[list[BindingType]] = [Binding("y", "copy", show=False)]

    def __init__(self, state: TaskState, number: str) -> None:
        super().__init__(reason_title(state, number), REASON_HINT, reason_text(state))

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self.text)
        self.app.notify(copied_note(self.text))
