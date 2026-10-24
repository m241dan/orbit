"""Building blocks of the orbit screen."""

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.visual import VisualType
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from orbit.protocol import Settings
from orbit.tui.modal import Push
from orbit.tui.view.common import (
    SPINNER_FPS,
    ListHints,
    kept_highlight,
    option_id,
    picked_row,
    picker_hint,
)
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


class NoteLine(Static):
    """Line showing a note, hidden while there's none."""

    DEFAULT_CSS = """
    NoteLine { display: none; }
    """

    def show(self, note: str | None) -> None:
        """Show ``note``, or hide the line when there's none."""
        self.update(note or "")
        self.display = note is not None

    @property
    def shown(self) -> str | None:
        """The note shown, or None while the line is hidden."""
        return str(self.render()) if self.display else None


class StatusLine(NoteLine):
    """Line by the prompt showing a status, hidden while there's none, coloured as a warning,
    or as an error with the ``error`` class."""

    DEFAULT_CSS = """
    StatusLine {
        color: $warning;
        padding: 0 1;
    }
    StatusLine.error { color: $error; }
    """


def relist(
    options: OptionList,
    labels: "Sequence[Option | VisualType]",
    old_keys: Sequence[str | None],
    new_keys: Sequence[str | None],
) -> None:
    """List ``labels``, the rows with ``new_keys``, in ``options`` in place of the rows with
    ``old_keys``, the highlight going where ``kept_highlight`` says."""
    highlight = kept_highlight(old_keys, options.highlighted, new_keys)
    options.clear_options()
    options.add_options(labels)
    options.highlighted = highlight


class BoxedModal[T](ModalScreen[T]):
    """Modal screen whose text boxes are bordered in the secondary colour, or heavy in the
    accent colour while focused."""

    DEFAULT_CSS = """
    BoxedModal Input { border: round $secondary; }
    BoxedModal Input:focus { border: heavy $accent; }
    """


class ListScreen[T](BoxedModal[T]):
    """Modal screen around an ``OptionList``, whose highlight ``j`` and ``k`` move alongside
    the list's own arrow keys; a focused input handles keys first, so takes them as typing.
    A screen that yields ``_note_line`` under its list shows there why a change was refused."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "move('down')", "Down", show=False),
        Binding("k", "move('up')", "Up", show=False),
    ]

    def action_move(self, direction: str) -> None:
        options = self.query_one(OptionList)
        if direction == "down":
            options.action_cursor_down()
        else:
            options.action_cursor_up()

    def _relist(
        self,
        shown: Sequence[str | None],
        keys: Sequence[str | None],
        labels: Sequence[VisualType],
    ) -> None:
        """List ``labels``, the rows with ``keys``, in place of the rows with keys ``shown``,
        the highlight going where ``kept_highlight`` says."""
        relist(self.query_one(OptionList), self._options(keys, labels), shown, keys)

    @staticmethod
    def _options(keys: Sequence[str | None], labels: Sequence[VisualType]) -> list[Option]:
        """The options showing ``labels``, each with the id of its row's key in ``keys``."""
        return [Option(label, id=option_id(key)) for key, label in zip(keys, labels, strict=True)]

    @staticmethod
    def _picked_row(event: OptionList.OptionSelected, keys: Sequence[str | None]) -> int | None:
        """Which of the rows with ``keys`` the user picked with ``event``, by its option's id
        rather than its place, as the list may have changed since; none once it's gone."""
        return picked_row(event.option_id, keys)

    def _note_line(self) -> StatusLine:
        """The note under the list, for why a change was refused, hidden until there's one."""
        return StatusLine("", id="picker-note", classes="error")

    @property
    def note(self) -> str | None:
        """Why a change was refused, while that's shown under the list."""
        return self.query_one("#picker-note", StatusLine).shown

    def _refused(self, why: str) -> None:
        """Show ``why`` a change was refused, as ``_show_refusal`` does; nothing once this
        screen is gone."""
        if self.is_attached:
            self._show_refusal(why)

    def _show_refusal(self, why: str) -> None:
        """Show ``why`` a change was refused under the list."""
        self._show_note(why)

    def _show_note(self, note: str | None) -> None:
        """Show ``note`` under the list, or hide it while it's none."""
        self.query_one("#picker-note", StatusLine).show(note)


class TypingListScreen[T](ListScreen[T]):
    """List modal in a bordered box, with a text box under the list that's hidden until the
    user types in it, a note under the list saying why a change was refused, hidden until one
    is, and the keys ``HINTS`` names shown under the box: the text box's while it's shown, or
    else the list's, where escape quits at ``startup``.

    Escape leaves the text box, or else dismisses with ``nothing``, or quits orbit at
    ``startup``, where there's nothing to go back to.
    """

    AUTO_FOCUS = "OptionList"
    DEFAULT_CSS = """
    TypingListScreen { align: center middle; }
    TypingListScreen > Vertical {
        width: 90;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: round $accent;
        padding: 0 1;
    }
    TypingListScreen OptionList { height: auto; max-height: 30; border: none; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", show=False),
    ]
    HINTS: ClassVar[ListHints]

    def __init__(self, nothing: T, startup: bool) -> None:
        super().__init__()
        self.startup = startup
        self._nothing = nothing
        self._box_hint: str | None = None  # The text box's keys, while it's shown.

    @property
    def hint(self) -> str:
        """The keys shown under the box."""
        return picker_hint(self.HINTS, startup=self.startup, box=self._box_hint)

    def action_back(self) -> None:
        if self.query_one(Input).display:
            self._typing(None)
        elif self.startup:
            self.app.exit()
        else:
            self.dismiss(self._nothing)

    def _typing(self, box_hint: str | None, text: str = "") -> None:
        """Show the text box, starting from ``text``, with ``box_hint`` its keys, and type in
        it; or, with no keys, go back to the list. Either way, the box's own note is cleared."""
        box = self.query_one(Input)
        box.display = box_hint is not None
        box.border_subtitle = ""
        self._box_hint = box_hint
        self._show_hint()
        if box_hint is None:
            box.clear()
            self.query_one(OptionList).focus()
        else:
            box.value = text
            box.focus()

    def _show_refusal(self, why: str) -> None:
        """Show ``why`` a change was refused: in the text box's hint while it's shown, or else
        under the list."""
        box = self.query_one(Input)
        if box.display:
            box.border_subtitle = why
        else:
            self._show_note(why)

    def _show_hint(self) -> None:
        """Show the keys under the box."""
        self.query_one(Vertical).border_subtitle = self.hint


class PickerScreen(ListScreen[int | None]):
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

    def choose(self, current: int | None, chosen: Callable[[int], Awaitable[None] | None]) -> None:
        """Show the picker with option ``current`` highlighted and call ``chosen`` with the
        index picked; escape calls nothing."""
        self.current = current

        async def picked(index: int | None) -> None:
            if index is not None:
                result = chosen(index)
                if result is not None:
                    await result

        self.app.push_screen(self, picked)

    def on_screen_resume(self) -> None:
        self.query_one(OptionList).highlighted = self.current

    @on(OptionList.OptionSelected)
    def _select(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_index)

    def action_close(self) -> None:
        self.dismiss(None)


class ChoiceScreen(ListScreen[int]):
    """``title`` with ``message`` and the ``choices`` to pick from, the first preselected, in a
    box bordered in the accent colour, or the error colour with the ``error`` class; dismisses
    with the index of the one picked, or ``escape_index`` on escape."""

    AUTO_FOCUS = "OptionList"
    DEFAULT_CSS = """
    ChoiceScreen { align: center middle; }
    ChoiceScreen > Vertical {
        width: 70;
        max-width: 90%;
        height: auto;
        border: round $accent;
        padding: 0 1;
    }
    ChoiceScreen.error > Vertical { border: round $error; }
    ChoiceScreen OptionList { height: auto; border: none; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "escape", show=False),
    ]

    def __init__(
        self,
        title: str,
        message: str,
        choices: Sequence[str],
        *,
        escape_index: int,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.message = message
        self.choices = tuple(choices)
        self.escape_index = escape_index

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = self.title_text
            yield Static(Text(self.message), id="choice-message")
            yield OptionList(*self.choices)

    def on_mount(self) -> None:
        # One dismissed before it mounts, replaced by a newer one, has no list left.
        for options in self.query(OptionList):
            options.highlighted = 0

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_index)

    def action_escape(self) -> None:
        self.dismiss(self.escape_index)


class TextScreen(ModalScreen[None]):
    """``text`` in full, titled ``title`` with the keys in ``hint``: j/k and the arrow keys
    scroll it and escape closes it. Subclasses add their own keys and may recolour the border."""

    AUTO_FOCUS = None
    DEFAULT_CSS = """
    TextScreen { align: center middle; }
    TextScreen > VerticalScroll {
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
        Binding("escape", "close", show=False),
    ]

    def __init__(self, title: str, hint: str, text: str) -> None:
        super().__init__()
        self.title_text = title
        self.hint = hint
        self.text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll(can_focus=False) as box:
            box.border_title = self.title_text
            box.border_subtitle = self.hint
            yield Static(Text(self.text), id="text-body")

    def show(self, text: str) -> None:
        """Show ``text`` in place of the text shown, or to be shown once composed."""
        self.text = text
        for body in self.query("#text-body").results(Static):
            body.update(Text(text))

    def action_scroll(self, step: int) -> None:
        self.query_one(VerticalScroll).scroll_relative(y=step, animate=False)

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


def confirm(
    push: Push[ConfirmScreen, bool],
    title: str,
    message: str,
    yes: str,
    then: Callable[[], Awaitable[object]],
) -> None:
    """Ask ``title`` with ``message`` with ``push``, ``yes`` naming what ``y`` does, and await
    ``then`` only if the user answers yes."""

    async def answered(confirmed: bool | None) -> None:
        if confirmed:
            await then()

    push(ConfirmScreen(title, message, yes), answered)
