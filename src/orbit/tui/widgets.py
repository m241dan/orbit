"""Building blocks of the orbit screen."""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, cast, get_args

from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.css.styles import RulesMap
from textual.screen import ModalScreen
from textual.visual import VisualType
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static, Tab, Tabs, TextArea

from orbit.protocol import Event, Settings, StepState, UsageChanged
from orbit.tui.view import (
    ALL_TAB_LABEL,
    PINNED_TABS,
    PLANNER,
    SPINNER_FPS,
    ConversationTab,
    EntryKind,
    collapsible,
    entry_label,
    gutter,
    mark_planner_busy,
    mark_tab_read,
    mark_tab_unread,
    settings_summary,
    spinner_frame,
    starts_collapsed,
    step_lines,
    tab_key,
    tab_labels,
    track_roles,
    track_tabs,
    usage_lines,
)

if TYPE_CHECKING:
    _WidgetMixin = Widget
else:
    # Not a Widget at runtime, so Textual still inherits the base's CSS and component classes.
    _WidgetMixin = object


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


class ListPane(ModalFocus, OptionList):
    """A window's list that only takes focus once activated (``enter_list_mode``);
    then j/k move and escape returns to window navigation."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("escape", "normal_mode", show=False),
    ]

    def enter_list_mode(self) -> None:
        if self._activate() and self.highlighted is None and self.option_count:
            self.highlighted = 0

    def replace_prompt(self, index: int, prompt: VisualType) -> None:
        """Replace the prompt of the option at ``index``, redrawing only that option and, if
        its height changed, re-measuring only it and the options after it."""
        # OptionList.replace_option_prompt_at_index clears every option's render and line
        # caches, so the next draw wraps the whole list again. Textual has no public way to
        # invalidate one option, so this reaches into its private caches; the tests in
        # tests/test_list_pane.py guard them.
        option = self.get_option_at_index(index)
        render_cache = self._option_render_cache
        cached = render_cache.keys()  # LRUCache isn't iterable itself.
        for key in [key for key in cached if key[0] is option]:
            render_cache.discard(key)
        option._set_prompt(prompt)
        line_cache = self._line_cache
        height = line_cache.heights.get(index)
        if height is None:  # Not measured yet, so measured as the list is laid out.
            self.refresh()
            return
        width = self.scrollable_content_region.width - self._get_left_gutter_width()
        width -= self.get_component_styles("option-list--option").padding.width
        rules = cast(RulesMap, self.styles)  # As OptionList measures its options.
        if self._get_visual(option).get_height(rules, width) + option._divider != height:
            self._remeasure_from(index)
        self.refresh()

    def _remeasure_from(self, index: int) -> None:
        """Forget the lines of the options from ``index`` on and measure them again, keeping
        those before it; part of ``replace_prompt``, on the same private caches."""
        line_cache = self._line_cache
        del line_cache.lines[line_cache.index_to_line[index] :]
        for later in range(index, self.option_count):
            line_cache.heights.pop(later, None)
            line_cache.index_to_line.pop(later, None)
        self._update_lines()


Entry = tuple[str, EntryKind]
"""A transcript entry: its text and how it's styled."""


@dataclass(frozen=True)
class GutteredEntry:
    """A transcript entry as drawn: its ``label`` wrapped to the width left beside the
    ``gutter``, which goes in front of every row in ``gutter_style``, then a blank row so the
    entry reads as a paragraph."""

    label: Text
    gutter: str
    gutter_style: Style

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width - len(self.gutter)
        for row in console.render_lines(self.label, options.update_width(width), pad=False):
            yield Segment(self.gutter, self.gutter_style)
            yield from row
            yield Segment.line()
        yield Segment.line()


class TranscriptLog(ListPane):
    """One tab's entries, each an option: activated, j/k move between them and enter collapses
    or expands one. Kept at no display until shown, since the list lays out only what's on
    screen, so switching tabs is instant and adding an entry costs the same however long the
    conversation gets. The cursor's entry is marked in the left gutter rather than highlighted."""

    DEFAULT_CSS = """
    TranscriptLog {
        display: none;
        height: 1fr;
        max-height: 100%;
        border: none;
        padding: 0;
        background: transparent;
        scrollbar-gutter: stable;
    }
    TranscriptLog:focus { border: none; background-tint: $foreground 0%; }
    TranscriptLog.-shown { display: block; }
    TranscriptLog .transcript--user { color: $text-accent; text-style: bold; }
    TranscriptLog .transcript--tool { color: $text-muted; }
    TranscriptLog .transcript--verify { color: $text-warning; }
    TranscriptLog .transcript--agent { color: $text-accent; }
    TranscriptLog .transcript--request { color: $text-accent; }
    TranscriptLog .transcript--error { color: $text-error; }
    TranscriptLog .transcript--gutter { color: $text-muted; }
    TranscriptLog .transcript--marker { color: $accent; }
    TranscriptLog > .option-list--option-highlighted,
    TranscriptLog:focus > .option-list--option-highlighted,
    TranscriptLog > .option-list--option-hover {
        color: $foreground;
        background: transparent;
        text-style: none;
    }
    """
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "transcript--gutter",
        "transcript--marker",
        *(f"transcript--{kind}" for kind in get_args(EntryKind)),
    }

    def __init__(self, shown: bool = False) -> None:
        super().__init__()
        self._entries: list[Entry] = []
        self._collapsed: list[bool] = []
        self._marked: int | None = None  # The entry drawn with the marker.
        self.set_class(shown, "-shown")

    @property
    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    def enter_list_mode(self) -> None:
        """Activate the list, starting at the latest entry when it holds no cursor."""
        if self.highlighted is None and self.option_count:
            self.highlighted = self.option_count - 1
        super().enter_list_mode()

    def append(self, text: str, kind: EntryKind) -> None:
        """Add an entry, collapsed if its kind starts so; the view stays pinned to the latest
        entry unless the list is active or holds a cursor."""
        collapsed = starts_collapsed(kind) and collapsible(text)
        self._entries.append((text, kind))
        self._collapsed.append(collapsed)
        self.add_option(self._prompt(len(self._entries) - 1))
        if not self.has_focus and self.highlighted is None:
            self.scroll_end(animate=False)

    def release_cursor(self) -> None:
        """Let go of the held cursor, so the view follows the latest entry again."""
        self.highlighted = None
        self.scroll_end(animate=False)

    def show_place(self) -> None:
        """Scroll to the held cursor's entry, or to the latest entry if there's none."""
        if self.highlighted is None:
            self.scroll_end(animate=False)
        else:
            self.scroll_to_highlight()

    def collapsed(self, index: int) -> bool:
        return self._collapsed[index]

    def toggle(self, index: int) -> None:
        """Collapse the entry at ``index`` if it's expanded, and the other way round; an entry
        of one line stays as it is."""
        text, _ = self._entries[index]
        if not collapsible(text):
            return
        self._collapsed[index] = not self._collapsed[index]
        self._redraw(index)

    def watch_highlighted(self, highlighted: int | None) -> None:
        """Move the marker from the entry it was on to the cursor's, if there is one."""
        super().watch_highlighted(highlighted)
        previous, self._marked = self._marked, highlighted
        for index in (previous, highlighted):
            if index is not None:
                self._redraw(index)

    @on(OptionList.OptionSelected)
    def _toggle(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.toggle(event.option_index)

    def _redraw(self, index: int) -> None:
        self.replace_prompt(index, self._prompt(index))

    def _prompt(self, index: int) -> GutteredEntry:
        # Partial styles, so no background of their own paints a band across the entry.
        text, kind = self._entries[index]
        marked = index == self._marked
        style = self.get_component_rich_style(f"transcript--{kind}", partial=True)
        gutter_style = self.get_component_rich_style(
            "transcript--marker" if marked else "transcript--gutter", partial=True
        )
        label = Text(entry_label(text, self._collapsed[index]), style=style)
        return GutteredEntry(label, gutter(marked), gutter_style)


class Transcript(Vertical):
    """Main display: the running conversation, pinned to the latest output unless the shown
    tab's log holds a cursor.

    Entries are kept per tab (All, and the tab they belong to), each tab in a
    :class:`TranscriptLog` of its own, one of which is shown.
    """

    # Windows are reached with h/j/k/l, not by clicking; the mouse wheel still scrolls.
    can_focus = False

    DEFAULT_CSS = """
    Transcript {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.only: str | None = None  # Show only this tab's entries, or all.
        self._logs: dict[str | None, TranscriptLog] = {}

    def compose(self) -> ComposeResult:
        yield self._new_log(None, shown=True)

    def on_mount(self) -> None:
        self.border_title = "Conversation"

    @property
    def shown(self) -> TranscriptLog:
        """The log of the tab being shown."""
        return self._logs[self.only]

    def entries(self, key: str | None) -> tuple[Entry, ...]:
        """The entries under the tab keyed ``key``; None is All."""
        return self._logs[key].entries if key in self._logs else ()

    def add_entry(self, text: str, kind: EntryKind, key: str | None = None) -> None:
        """Add an entry under All and, if ``key`` names a tab, under that tab."""
        for tab in {None, key}:
            self._log(tab).append(text, kind)

    def enter_list_mode(self) -> None:
        """Activate the shown log: j/k move between its entries, enter collapses or expands
        one, and escape leaves."""
        self.shown.enter_list_mode()

    def release_cursor(self) -> None:
        """Let go of the shown log's cursor, so it follows the latest entry again."""
        self.shown.release_cursor()

    def show_only(self, key: str | None, label: str = "") -> None:
        """Show only the tab keyed ``key``'s entries, titled by its tab ``label``, or all."""
        if self.shown.has_focus:
            self.screen.set_focus(None)
        self.shown.remove_class("-shown")
        self.only = key
        self.border_title = "Conversation" if key is None else f"Conversation · {label}"
        log = self._log(key)
        log.add_class("-shown")
        # Laid out once shown, which is when its end is known.
        self.call_after_refresh(log.show_place)

    def _log(self, key: str | None) -> TranscriptLog:
        if key not in self._logs:
            self.mount(self._new_log(key))
        return self._logs[key]

    def _new_log(self, key: str | None, shown: bool = False) -> TranscriptLog:
        log = TranscriptLog(shown=shown)
        self._logs[key] = log
        return log


class ConversationTabs(Tabs):
    """Strip of tabs above the transcript: "All", a pinned tab per built-in role, then one per
    running agent in any other role.

    Clicking a tab activates it; like the transcript, it never takes focus.
    """

    can_focus = False

    ALL_TAB_ID = "tab-all"
    PLANNER_TAB_ID = f"tab-{PLANNER}"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            Tab(ALL_TAB_LABEL, id=self.ALL_TAB_ID),
            *(Tab(tab.role, id=f"tab-{tab.key}") for tab in PINNED_TABS),
            **kwargs,
        )
        self.tabs: tuple[ConversationTab, ...] = PINNED_TABS
        self.roles: Mapping[str, str] = {}  # Agents that ran in a pinned role, to the role.
        # Tab key (an agent id, or a pinned role's) to its tab's id.
        self._tab_ids: dict[str, str] = {tab.key: f"tab-{tab.key}" for tab in PINNED_TABS}
        self._next_id = 1
        self._before_planner = self.ALL_TAB_ID  # Where ctrl+t goes back to from the planner.

    def agent_of(self, tab_id: str) -> str | None:
        """The key of the tab with id ``tab_id``: an agent's id, a pinned role's, or None for
        All."""
        return next((agent for agent, tid in self._tab_ids.items() if tid == tab_id), None)

    def key_of(self, agent: str | None) -> str | None:
        """The key of the tab ``agent``'s entries belong to."""
        return tab_key(agent, self.roles)

    def toggle_planner(self) -> None:
        """Show the planner tab, or from there, the tab shown before it (All if it's gone)."""
        if self.active != self.PLANNER_TAB_ID:
            self._before_planner = self.active
            self.active = self.PLANNER_TAB_ID
        else:
            # Back to the tab we came from (All included), or All if that tab has since closed.
            open_tabs = {self.ALL_TAB_ID, *self._tab_ids.values()}
            back = self._before_planner
            self.active = back if back in open_tabs else self.ALL_TAB_ID

    def track(self, event: Event) -> None:
        """Add a tab for an agent that started outside the pinned roles, remove it when that
        agent exits, and mark a tab not being shown when one of its agents has new output."""
        self.roles = track_roles(self.roles, event)
        self._update(track_tabs(self.tabs, event, self.agent_of(self.active), self.roles))

    def mark_read(self, agent: str | None) -> None:
        """Clear ``agent``'s unread marker; None (All) has none."""
        if agent is not None:
            self._update(mark_tab_read(self.tabs, agent))

    def mark_unread(self, key: str | None) -> None:
        """Mark the tab keyed ``key`` unread, as an entry with no agent landed in it, unless
        it's being shown; None (All) has no marker."""
        self._update(mark_tab_unread(self.tabs, key, self.agent_of(self.active)))

    def set_planner_busy(self, busy: bool) -> None:
        """Show the planner tab's running marker while the planner is ``busy`` answering."""
        self._update(mark_planner_busy(self.tabs, busy))

    def _update(self, tabs: tuple[ConversationTab, ...]) -> None:
        if tabs == self.tabs:
            return
        for gone in set(self._tab_ids) - {t.key for t in tabs}:
            self._remove(gone)
        for tab, label in zip(tabs, tab_labels(tabs), strict=True):
            if tab.key in self._tab_ids:
                self._relabel(self._tab_ids[tab.key], label)
            else:
                self._add(tab.key, label)
        self.tabs = tabs

    def _add(self, agent: str, label: str) -> None:
        tab_id = f"agent-tab-{self._next_id}"
        self._next_id += 1
        self._tab_ids[agent] = tab_id
        self.add_tab(Tab(label, id=tab_id))

    def _remove(self, agent: str) -> None:
        """Remove ``agent``'s tab, showing All instead if it was active."""
        tab_id = self._tab_ids.pop(agent)
        if self.active == tab_id:
            self.active = self.ALL_TAB_ID
        self.remove_tab(tab_id)

    def _relabel(self, tab_id: str, label: str) -> None:
        tab = self.query_one(f"#{tab_id}", Tab)
        if tab.label_text != label:
            tab.label = label
            if self.active == tab_id:
                # Let the transcript's title follow the new label.
                self.post_message(self.TabActivated(self, tab))


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


NO_STEPS_TEXT = "No verification steps"


class VerificationPanel(SidePanel):
    """The verification steps and how each fared after the current task, as reported.

    A running step spins and shows how long it has been running, so a slow step
    doesn't look stalled.
    """

    def __init__(self, *, id: str) -> None:
        super().__init__("Verification steps", id=id)
        self.steps: tuple[StepState, ...] = ()
        self.title = ""  # What the steps check, when it isn't the current task.
        self.clock: Callable[[], float] = time.monotonic
        # The running step and when it started, by ``clock``.
        self._running_step: int | None = None
        self._since = 0.0
        self._text = NO_STEPS_TEXT  # What the panel shows now.
        self._height: int | None = None  # Its rows when last drawn, if its width was known.

    def compose(self) -> ComposeResult:
        yield Static(NO_STEPS_TEXT)

    def on_mount(self) -> None:
        self.set_interval(1 / SPINNER_FPS, self.tick)

    def show(self, steps: Sequence[StepState], title: str = "") -> None:
        self.steps, self.title = tuple(steps), title
        running = next((i for i, s in enumerate(self.steps) if s.status == "running"), None)
        if running is not None and running != self._running_step:
            self._since = self.clock()
        self._running_step = running
        self.tick()

    def tick(self) -> None:
        """Redraw the steps, if they or the spinner's frame changed since the last time."""
        now = self.clock()
        lines = step_lines(self.steps, spinner_frame(now), int(now - self._since), self.title)
        text = "\n".join(lines) or NO_STEPS_TEXT
        if text != self._text:
            # Spinner frames keep every line's length, so only a change in the wrapped
            # height needs a layout.
            height = self._wrapped_height(text)
            layout = height is None or height != self._height
            self._text, self._height = text, height
            self.query_one(Static).update(Text(text), layout=layout)

    def _wrapped_height(self, text: str) -> int | None:
        """The rows ``text`` takes wrapped to the panel's width, or None before it has one."""
        width = self.query_one(Static).content_region.width
        if not width:
            return None
        return len(Text(text).wrap(self.app.console, width))


def _local_now() -> datetime:
    return datetime.now().astimezone()


class UsagePanel(SidePanel):
    """How much of each subscription usage limit is used, as reported; hidden until a report
    arrives, so the layout is unchanged when nothing reports usage."""

    DEFAULT_CSS = """
    UsagePanel { height: auto; }
    """

    def __init__(self, *, id: str) -> None:
        super().__init__("Usage", id=id)
        self.display = False
        self.event: UsageChanged | None = None
        self.now: Callable[[], datetime] = _local_now

    def compose(self) -> ComposeResult:
        yield Static()

    def on_mount(self) -> None:
        # Keeps the reset times right as the day changes.
        self.set_interval(60, self.tick)

    def on_resize(self) -> None:
        self.tick()

    def show(self, event: UsageChanged) -> None:
        self.event = event
        self.display = True
        self.tick()

    def tick(self) -> None:
        """Redraw the usage reported, if any, to fit the panel's width."""
        if self.event is None:
            return
        lines = usage_lines(self.event, self.content_region.width, self.now())
        self.query_one(Static).update(Text("\n").join(lines))


class PromptInput(ModalFocus, Input):
    """Single-line prompt box at the bottom of the screen.

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

    def on_mount(self) -> None:
        self.border_title = "Prompt · planner"

    def enter_insert_mode(self) -> None:
        self._activate()

    def show_settings(self, settings: Settings) -> None:
        self.border_subtitle = settings_summary(settings)


class PromptBox(ModalFocus, TextArea):
    """Multi-line prompt box, for a task's whole prompt.

    Vim-style like ``PromptInput``: it only takes focus in insert mode
    (``enter_insert_mode``), which puts the cursor at the end so typing appends, and
    escape or losing focus returns to normal mode, where keys go to the app.
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

    def enter_insert_mode(self) -> None:
        if self._activate():
            self.move_cursor(self.document.end)


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


CONFIGURE_TITLE = "Set up checks?"
CONFIGURE_MESSAGE = "A configurator task will go to the front of the task list and run now."
CONFIGURE_YES = "set up"


class ConfirmScreen(ModalScreen[bool]):
    """Asks ``title`` with ``message``; dismisses with True on ``y``, False on ``n`` or escape.

    ``yes`` names what ``y`` does in the keys shown. Enter doesn't answer, so a stray
    keypress can't confirm.
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

    def __init__(self, title: str, message: str, yes: str = "confirm") -> None:
        super().__init__()
        self.title_text = title
        self.message = message
        self.yes = yes

    def compose(self) -> ComposeResult:
        body = Static(Text(self.message), id="confirm-message")
        body.border_title = self.title_text
        body.border_subtitle = f"y {self.yes} · n/esc cancel"
        yield body

    def action_answer(self, confirmed: bool) -> None:
        self.dismiss(confirmed)
