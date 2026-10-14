"""Building blocks of the orbit screen."""

import time
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static, Tab, Tabs

from orbit.protocol import Event, Settings, StepState
from orbit.tui.view import (
    ALL_TAB_LABEL,
    SPINNER_FPS,
    ConversationTab,
    EntryKind,
    mark_tab_read,
    settings_summary,
    spinner_frame,
    step_lines,
    tab_labels,
    track_tabs,
)


class Entry(Static):
    """One item in the transcript."""

    DEFAULT_CSS = """
    Entry { margin: 0 0 1 0; }
    Entry.user { color: $text-accent; text-style: bold; }
    Entry.tool { color: $text-muted; }
    Entry.verify { color: $text-warning; }
    Entry.agent { color: $text-accent; }
    Entry.error { color: $text-error; }
    """

    def __init__(self, text: str, kind: EntryKind, agent: str | None = None) -> None:
        super().__init__(Text(text), classes=kind)
        self.agent = agent


class Transcript(VerticalScroll):
    """Main display: the running conversation, pinned to the latest output."""

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
        self.only: str | None = None  # Show only this agent's entries, or all.

    def on_mount(self) -> None:
        self.border_title = "Conversation"

    def add_entry(self, text: str, kind: EntryKind, agent: str | None = None) -> None:
        entry = Entry(text, kind, agent)
        entry.display = self.only is None or agent == self.only
        self.mount(entry)
        self.scroll_end(animate=False)

    def show_only(self, agent: str | None, label: str = "") -> None:
        """Show only ``agent``'s entries, titled by its tab ``label``, or all of them."""
        self.only = agent
        self.border_title = "Conversation" if agent is None else f"Conversation · {label}"
        for entry in self.query(Entry):
            entry.display = agent is None or entry.agent == agent
        self.scroll_end(animate=False)


class ConversationTabs(Tabs):
    """Strip of tabs above the transcript: "All", then one per running agent.

    Clicking a tab activates it; like the transcript, it never takes focus.
    """

    can_focus = False

    ALL_TAB_ID = "tab-all"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(Tab(ALL_TAB_LABEL, id=self.ALL_TAB_ID), **kwargs)
        self.tabs: tuple[ConversationTab, ...] = ()
        self._tab_ids: dict[str, str] = {}  # Agent id to its tab's id.
        self._next_id = 1

    def agent_of(self, tab_id: str) -> str | None:
        """The agent whose tab has id ``tab_id``; None for All."""
        return next((agent for agent, tid in self._tab_ids.items() if tid == tab_id), None)

    def track(self, event: Event) -> None:
        """Add a tab for an agent that started, remove an exited agent's tab, and mark a tab
        not being shown when its agent has new output."""
        self._update(track_tabs(self.tabs, event, self.agent_of(self.active)))

    def mark_read(self, agent: str | None) -> None:
        """Clear ``agent``'s unread marker; None (All) has none."""
        if agent is not None:
            self._update(mark_tab_read(self.tabs, agent))

    def _update(self, tabs: tuple[ConversationTab, ...]) -> None:
        if tabs == self.tabs:
            return
        for gone in set(self._tab_ids) - {t.agent_id for t in tabs}:
            self._remove(gone)
        for tab, label in zip(tabs, tab_labels(tabs), strict=True):
            if tab.agent_id in self._tab_ids:
                self._relabel(self._tab_ids[tab.agent_id], label)
            else:
                self._add(tab.agent_id, label)
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
        self.clock: Callable[[], float] = time.monotonic
        # The running step and when it started, by ``clock``.
        self._running_step: int | None = None
        self._since = 0.0

    def compose(self) -> ComposeResult:
        yield Static(NO_STEPS_TEXT)

    def on_mount(self) -> None:
        self.set_interval(1 / SPINNER_FPS, self.tick)

    def show(self, steps: Sequence[StepState]) -> None:
        self.steps = tuple(steps)
        running = next((i for i, s in enumerate(self.steps) if s.status == "running"), None)
        if running is not None and running != self._running_step:
            self._since = self.clock()
        self._running_step = running
        self.tick()

    def tick(self) -> None:
        now = self.clock()
        lines = step_lines(self.steps, spinner_frame(now), int(now - self._since))
        self.query_one(Static).update(Text("\n".join(lines) or NO_STEPS_TEXT))


class PromptInput(Input):
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
        self.can_focus = False

    def on_mount(self) -> None:
        self.border_title = "Prompt"

    def enter_insert_mode(self) -> None:
        if not self.disabled:
            self.can_focus = True
            self.focus()

    def action_normal_mode(self) -> None:
        self.screen.set_focus(None)

    def on_blur(self) -> None:
        self.can_focus = False

    def show_settings(self, settings: Settings) -> None:
        self.border_subtitle = settings_summary(settings)

    def show_planning(self, on: bool) -> None:
        self.border_title = "Prompt · planner" if on else "Prompt"


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
