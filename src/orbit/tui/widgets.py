"""Building blocks of the orbit screen."""

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static, TextArea

from orbit.protocol import Settings
from orbit.tui.view.common import SPINNER_FPS
from orbit.tui.view.confirm import confirm_hint
from orbit.tui.view.settings import settings_summary
from orbit.tui.view.transcript import prompt_title

if TYPE_CHECKING:
    _WidgetMixin = Widget
else:
    # Not a Widget at runtime, so Textual still inherits the base's CSS and component classes.
    _WidgetMixin = object


class Ticker:
    """Calls ``tick`` as fast as spinners turn, on ``owner``'s timer, but only while kept
    running, so nothing redraws while nothing spins."""

    def __init__(self, owner: Widget, tick: Callable[[], None]) -> None:
        self._owner = owner
        self._tick = tick
        self._timer: Timer | None = None

    @property
    def ticking(self) -> bool:
        """Whether the timer runs."""
        return self._timer is not None

    def keep(self, running: bool) -> None:
        """Tick while ``running``, starting the timer if it's stopped, and stop it if not."""
        if running and self._timer is None:
            self._timer = self._owner.set_interval(1 / SPINNER_FPS, self._tick)
        elif not running and self._timer is not None:
            self._timer.stop()
            self._timer = None


def mark_selected(widget: Widget, selected: bool) -> None:
    """Mark ``widget`` selected with the ``selected`` class, or unmark it, restyling only it."""
    # The class styles the widget's own border, but a class change restyles all the widget
    # holds, such as each of the conversation's logs and their component classes.
    widget.set_class(selected, "selected", update=False)
    widget.app.stylesheet.apply(widget)


class ModalFocus(_WidgetMixin):
    """Vim-style focus, mixed into a widget before its Textual base: the widget only takes
    focus once activated, and escape (bound by the widget to ``normal_mode``) or losing
    focus makes it unfocusable again, returning keys to the app."""

    can_focus = False

    def _activate(self) -> bool:
        """Whether the widget took focus; a disabled one doesn't."""
        if self.disabled:
            return False
        self.can_focus = True
        self.focus()
        return True

    def action_normal_mode(self) -> None:
        self.screen.set_focus(None)

    def on_blur(self) -> None:
        self.can_focus = False


class SidePanel(VerticalScroll):
    """Titled pane in the right-hand column."""

    can_focus = False

    DEFAULT_CSS = """
    SidePanel {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    """

    def __init__(self, title: str, *, id: str) -> None:
        super().__init__(id=id)
        self.border_title = title


class PromptInput(ModalFocus, Input):
    """Single-line input, for a task's title.

    Vim-style: it only takes focus in insert mode (``enter_insert_mode``), and
    escape or losing focus returns to normal mode, where keys go to the app.
    """

    DEFAULT_CSS = """
    PromptInput {
        height: 3;
        border: round $secondary;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "normal_mode", "Normal mode", show=False)
    ]

    def __init__(self, **kwargs: Any) -> None:
        # Keep the draft unselected so re-entering insert mode appends to it.
        super().__init__(select_on_focus=False, **kwargs)

    def enter_insert_mode(self) -> None:
        self._activate()


class ModalTextArea(ModalFocus, TextArea):
    """Text area with Vim-style focus: it only takes focus in insert mode
    (``enter_insert_mode``), which puts the cursor at the end so typing appends."""

    def enter_insert_mode(self) -> None:
        if self._activate():
            self.move_cursor(self.document.end)


class ChatPrompt(ModalTextArea):
    """Prompt box at the bottom of the screen, which wraps its text and grows with it from
    one row up to eight, then scrolls; enter sends it, and pasted newlines are kept.

    Vim-style like ``ModalTextArea``: escape or losing focus returns to normal mode.
    """

    DEFAULT_CSS = """
    ChatPrompt {
        height: auto;
        max-height: 10;
        border: round $secondary;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "normal_mode", "Normal mode", show=False),
        # Priority, to beat the text area's own enter, which types a newline.
        Binding("enter", "submit", show=False, priority=True),
    ]

    class Submitted(Message):
        """The user sent ``text``."""

        def __init__(self, prompt: "ChatPrompt", text: str) -> None:
            super().__init__()
            self.prompt = prompt
            self.text = text

        @property
        def control(self) -> "ChatPrompt":
            return self.prompt

    def __init__(self, *, placeholder: str = "", id: str | None = None) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False, placeholder=placeholder, id=id)
        self._busy = False
        self._read_only = False

    def on_mount(self) -> None:
        self.show_busy(False)

    def show_busy(self, busy: bool) -> None:
        self._busy = busy
        self.border_title = prompt_title(busy, self._read_only)

    def show_read_only(self, read_only: bool) -> None:
        """Mark the prompt while the orbit is ``read_only``."""
        self._read_only = read_only
        self.border_title = prompt_title(self._busy, read_only)

    def show_settings(self, settings: Settings) -> None:
        self.border_subtitle = settings_summary(settings)

    def action_submit(self) -> None:
        self.post_message(self.Submitted(self, self.text))


class PromptBox(ModalTextArea):
    """Multi-line prompt box, for a task's whole prompt.

    Vim-style like ``ModalTextArea``: escape or losing focus returns to normal mode, where
    keys go to the app.
    """

    DEFAULT_CSS = """
    PromptBox {
        height: 12;
        border: round $secondary;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "normal_mode", "Normal mode", show=False)
    ]

    def __init__(self, text: str = "", **kwargs: Any) -> None:
        super().__init__(text, soft_wrap=True, show_line_numbers=False, **kwargs)


class PickerScreen(ModalScreen[int | None]):
    """Pop-up list of options. Dismisses with the chosen index, or None on escape.

    Meant to be built once and installed so reopening it is instant.
    """

    DEFAULT_CSS = """
    PickerScreen { align: center middle; }
    PickerScreen > OptionList {
        width: 70;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        padding: 0 1;
    }
    PickerScreen > OptionList, PickerScreen > OptionList:focus { border: round $accent; }
    """
    AUTO_FOCUS = "OptionList"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close"),
        # Vim-style movement alongside the list's own arrow keys.
        Binding("j", "move('down')", "Down", show=False),
        Binding("k", "move('up')", "Up", show=False),
    ]

    def __init__(self, title_text: str, labels: Sequence[str]) -> None:
        super().__init__()
        self.title_text = title_text
        self._labels = list(labels)
        # Index to highlight each time the picker is shown.
        self.current: int | None = None

    def compose(self) -> ComposeResult:
        options = OptionList(*self._labels)
        options.border_title = self.title_text
        yield options

    def set_labels(self, labels: Sequence[str]) -> None:
        self._labels = list(labels)
        if self.is_mounted:
            options = self.query_one(OptionList)
            options.clear_options()
            options.add_options(self._labels)

    def choose[T](
        self,
        options: Sequence[T],
        current: int | None,
        chosen: Callable[[T], Awaitable[None] | None],
    ) -> None:
        """Show the picker with option ``current`` highlighted and call ``chosen`` with the
        item of ``options`` picked; escape calls nothing."""
        self.current = current

        async def picked(index: int | None) -> None:
            if index is not None:
                result = chosen(options[index])
                if result is not None:
                    await result

        self.app.push_screen(self, picked)

    def on_screen_resume(self) -> None:
        self.query_one(OptionList).highlighted = self.current

    @on(OptionList.OptionSelected)
    def _select(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_index)

    def action_move(self, direction: str) -> None:
        options = self.query_one(OptionList)
        if direction == "down":
            options.action_cursor_down()
        else:
            options.action_cursor_up()

    def action_close(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Asks ``title`` with ``message``; dismisses with True on ``y``, False on ``n`` or escape.

    ``yes`` names what ``y`` does in the keys shown, and ``no`` what ``n`` and escape do.
    Enter doesn't answer, so a stray keypress can't confirm.
    """

    AUTO_FOCUS = None
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Static {
        width: 60;
        max-width: 90%;
        height: auto;
        border: round $accent;
        padding: 0 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "answer(True)", show=False),
        Binding("n", "answer(False)", show=False),
        Binding("escape", "answer(False)", show=False),
    ]

    def __init__(self, title: str, message: str, yes: str = "confirm", no: str = "cancel") -> None:
        super().__init__()
        self.title_text = title
        self.message = message
        self.yes = yes
        self.no = no

    def compose(self) -> ComposeResult:
        body = Static(Text(self.message), id="confirm-message")
        body.border_title = self.title_text
        body.border_subtitle = confirm_hint(self.yes, self.no)
        yield body

    def action_answer(self, confirmed: bool) -> None:
        self.dismiss(confirmed)
