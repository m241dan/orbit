"""The detail pane: the full content of the tool call or request under the conversation's
cursor."""

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from orbit.tui.view.detail import DETAIL_HEIGHT, detail_title
from orbit.tui.view.detail_views import detail_body
from orbit.tui.view.tool_calls import ToolCall
from orbit.tui.widgets import ModalFocus


class DetailPane(ModalFocus, VerticalScroll):
    """The full content of the tool call or request under the conversation's cursor, below
    the logs and the working row, taking ``share`` percent of the conversation, or all of it
    while ``covering``; hidden until opened. Activated, j/k scroll it and escape leaves."""

    class Focused(Message):
        """The pane was activated."""

    DEFAULT_CSS = """
    DetailPane {
        display: none;
        border-top: solid $primary;
        border-title-align: left;
    }
    DetailPane.-open { display: block; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "scroll(1)", show=False),
        Binding("k", "scroll(-1)", show=False),
        Binding("escape", "normal_mode", show=False),
    ]

    share: reactive[int] = reactive(DETAIL_HEIGHT)
    covering: reactive[bool] = reactive(False)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.call: ToolCall | None = None  # The call whose details are shown, if any.

    def compose(self) -> ComposeResult:
        yield Static()

    def watch_share(self) -> None:
        self._fit()

    def watch_covering(self) -> None:
        self._fit()

    def _fit(self) -> None:
        self.styles.height = "1fr" if self.covering else f"{self.share}%"

    @property
    def opened(self) -> bool:
        return self.has_class("-open")

    def show(self, call: ToolCall | None) -> None:
        """Open, showing ``call``'s details, or a note that there are none; another call than
        the one shown starts at the top, while the same one updated keeps its place."""
        if not self.opened or self._another(call):
            self.scroll_home(animate=False)
        self.call = call
        self.border_title = detail_title(call)
        self.query_one(Static).update(detail_body(call))
        self.add_class("-open")

    def _another(self, call: ToolCall | None) -> bool:
        """Whether ``call`` is another call than the one shown, or none where one is."""
        if call is None or self.call is None:
            return call is not self.call
        return call.call_id != self.call.call_id

    def close(self) -> None:
        """Close, leaving the pane if it's active."""
        if self.has_focus:
            self.screen.set_focus(None)
        self.remove_class("-open")

    def enter_scroll_mode(self) -> None:
        """Activate the pane, so j/k scroll it."""
        self._activate()

    def on_focus(self) -> None:
        self.post_message(self.Focused())

    def action_scroll(self, step: int) -> None:
        """Scroll ``step`` lines."""
        self.scroll_relative(y=step, animate=False)
