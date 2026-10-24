"""The detail pane: the full content of the tool call or request under the conversation's
cursor."""

from typing import Any, ClassVar

from rich.console import RenderableType
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from orbit.tui.view.detail import DETAIL_HEIGHT
from orbit.tui.view.detail_views import Detail
from orbit.tui.widgets import ModalFocus


class DetailPane(ModalFocus, VerticalScroll):
    """Shows the :class:`Detail` of the tool call or request under the conversation's cursor,
    below the logs and the working row, taking ``share`` percent of the conversation, or all
    of it while ``covering``; hidden until opened. Activated, j/k scroll it and escape
    leaves."""

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
        self.detail = Detail(self)

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
        return self.detail.opened

    def scroll_to_top(self) -> None:
        self.scroll_home(animate=False)

    def show_detail(self, title: str, body: RenderableType) -> None:
        self.border_title = title
        self.query_one(Static).update(body)
        self.add_class("-open")

    def hide(self) -> None:
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
