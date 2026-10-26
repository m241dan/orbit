"""Fakes for the conversation's models: the window, logs, working row and detail pane they
show in, and a feed of events into them as the app routes them."""

import io
from collections.abc import Callable, Mapping, Sequence

from rich.console import Console, RenderableType

from orbit.protocol import (
    PLANNER,
    Event,
    RunningAgent,
    Snapshot,
    StepState,
    TaskState,
    Usage,
)
from orbit.tui.view.conversation import Conversation
from orbit.tui.view.detail_views import Detail
from orbit.tui.view.events import FEED_EVENTS, in_group
from orbit.tui.view.feed import ConversationFeed
from orbit.tui.view.log import EntryLog, EntryRow
from orbit.tui.view.tabs import running_roles, tab_key, track_roles
from orbit.tui.view.working import WorkingLines


def plain(renderable: RenderableType) -> list[str]:
    """``renderable``'s lines as drawn 40 columns wide."""
    out = io.StringIO()
    Console(width=40, file=out, color_system=None).print(renderable)
    return out.getvalue().splitlines()


class EntryList:
    """A tab's list, as a log's entries show in it: a row per entry shown, the cursor the user
    moves, whether it's active, how many times the log was rebuilt, scrolled to its end, and
    scrolled to its place, and how many times it said the detail pane may need to change."""

    def __init__(self, thinking_hidden: bool = False, clock: Callable[[], float] = lambda: 0.0):
        self.highlighted: int | None = None
        self.has_focus = False
        self.rows: list[EntryRow] = []
        self.rebuilds = 0
        self.ends = 0
        self.places = 0
        self.inspects = 0
        self.entry_log = EntryLog(self, thinking_hidden, clock)

    def move(self, row: int | None) -> None:
        """Put the cursor on option ``row``, as the user moves it, the log moving its marker as
        the list tells it whenever the cursor changes."""
        if row != self.highlighted:
            self.highlighted = row
            self.entry_log.moved(row)

    def highlight(self, row: int | None) -> None:
        self.move(row)

    def add_row(self, row: EntryRow) -> None:
        self.rows.append(row)

    def redraw(self, index: int, row: EntryRow) -> None:
        self.rows[index] = row

    def replace_rows(self, rows: Sequence[EntryRow], highlight: int | None) -> None:
        self.rebuilds += 1
        self.clear_rows()
        self.rows = list(rows)
        self.move(highlight)
        self.show_place()

    def clear_rows(self) -> None:
        self.rows = []
        self.move(None)

    def scroll_to_end(self) -> None:
        self.ends += 1

    def show_place(self) -> None:
        self.places += 1

    def detail_changed(self) -> None:
        self.inspects += 1

    @property
    def labels(self) -> list[str]:
        """The rows' labels, without their gutter."""
        return [row.label for row in self.rows]

    @property
    def gutters(self) -> list[str]:
        """The gutter symbol of each row."""
        return [row.gutter.strip() for row in self.rows]

    @property
    def headings(self) -> list[str | None]:
        """The label row heading each row, if it has one."""
        return [row.heading for row in self.rows]

    @property
    def brightness(self) -> list[str]:
        """How each agent's message drawn as markdown is drawn: dim or bright."""
        names = {"transcript--thinking": "dim", "transcript--assistant": "bright"}
        return [names[row.component] for row in self.rows if row.body == "markdown"]


class Row:
    """The working row as its lines show in it, ``width`` columns wide: what it shows each
    time, in turn."""

    def __init__(self, width: int = 80) -> None:
        self.width = width
        self.shows: list[tuple[str, ...]] = []

    def show_lines(self, lines: Sequence[str]) -> None:
        self.shows.append(tuple(lines))


class Pane:
    """The detail pane as a call's details show in it: its title and body, whether it's open,
    and how many times it was scrolled back to the top."""

    def __init__(self) -> None:
        self.title = ""
        self.body: list[str] = []
        self.open = False
        self.tops = 0

    def scroll_to_top(self) -> None:
        self.tops += 1

    def show_detail(self, title: str, body: RenderableType) -> None:
        self.title, self.body, self.open = title, plain(body), True

    def hide(self) -> None:
        self.open = False


class Log(EntryList):
    """A tab's log in a :class:`Window`: its entries' list, whether it's displayed, and
    whether it was removed; what it says of the detail pane reaches the window later, as a
    message does."""

    def __init__(self, window: Window, thinking_hidden: bool) -> None:
        super().__init__(thinking_hidden, window.clock)
        self._window = window
        self.displayed = False
        self.removed = False

    def show(self, shown: bool) -> None:
        self.displayed = shown

    def remove(self) -> None:
        self.removed = True

    def detail_changed(self) -> None:
        super().detail_changed()
        self._window.pending += 1


class Window:
    """The conversation window as the conversation shows in it, ``height`` rows high, with its
    logs, a working row and a detail pane: its title, whether the pane covers it, whether it
    ticks, what it copied and how often it left whatever was active; ``now`` is its clock."""

    def __init__(self, height: int = 30) -> None:
        self.now = 0.0
        self.content_height = height
        self.title = ""
        self.corner = ""
        self.covering = False
        self.ticking = False
        self.copies: list[tuple[str, str]] = []
        self.unfocused = 0
        self.pending = 0  # What the logs said of the detail pane, not yet taken in.
        self.logs: list[Log] = []
        self.row, self.pane = Row(), Pane()
        self.working = WorkingLines(self.row, self.clock)
        self.detail = Detail(self.pane)
        first = self.new_log(False)
        first.show(True)
        self.conversation = Conversation(self, first, self.working, self.detail)
        self.conversation.retitle()

    def clock(self) -> float:
        return self.now

    def new_log(self, thinking_hidden: bool) -> Log:
        log = Log(self, thinking_hidden)
        self.logs.append(log)
        return log

    def show_title(self, title: str) -> None:
        self.title = title

    def show_corner(self, text: str) -> None:
        self.corner = text

    def cover(self, covering: bool) -> None:
        self.covering = covering

    def keep_ticking(self, running: bool) -> None:
        self.ticking = running

    def unfocus(self) -> None:
        self.unfocused += 1
        for log in self.logs:
            log.has_focus = False

    def call_after_refresh(self, callback: Callable[[], object]) -> bool:
        callback()
        return True

    def copy(self, text: str, note: str) -> None:
        self.copies.append((text, note))

    def settle(self) -> None:
        """Take in what the logs said of the detail pane, as the window takes in their
        messages once what posted them is done."""
        while self.pending:
            self.pending -= 1
            self.conversation.inspect()

    @property
    def shown(self) -> Log:
        """The log shown."""
        return self.conversation.shown

    @property
    def displayed(self) -> list[Log]:
        """The logs displayed."""
        return [log for log in self.logs if log.displayed]


class Tabs:
    """The conversation's tabs, as the feed sees them: the pinned roles agents run in,
    tracked from events as the app's tabs track them, and the tabs marked unread."""

    def __init__(self) -> None:
        self.roles: Mapping[str, str] = {}
        self.unread: list[str | None] = []

    def track(self, event: Event) -> None:
        self.roles = track_roles(self.roles, event)

    def key_of(self, agent: str | None) -> str | None:
        return tab_key(agent, self.roles)

    def reset(self, running: Sequence[RunningAgent]) -> None:
        self.roles = running_roles(running)

    def replay(self, event: Event) -> None:
        self.track(event)

    def follow(self, running: Sequence[RunningAgent]) -> None:
        self.roles = {**self.roles, **running_roles(running)}

    def learn_roles(self, event: Event) -> None:
        self.track(event)

    def mark_unread(self, key: str | None) -> None:
        self.unread.append(key)


class Tasks:
    """The task list, as the feed sees it: the ``tasks`` listed, numbered from 1, and what the
    turns it was told of cost."""

    def __init__(self, *tasks: TaskState) -> None:
        self.tasks = tasks
        self.numbers_by_id = {task.id: str(n) for n, task in enumerate(tasks, 1)}
        self.counted: list[Usage] = []

    def find(self, task_id: str) -> TaskState | None:
        return next((task for task in self.tasks if task.id == task_id), None)

    def count(self, usage: Usage) -> None:
        self.counted.append(usage)


class Steps:
    """The verification ``steps``, as the feed names them."""

    def __init__(self, *steps: StepState) -> None:
        self.steps = steps


class Session:
    """A conversation in a :class:`Window`, fed events as the app routes them: the tabs follow
    each event, then the working row, then the feed shows it."""

    def __init__(
        self,
        height: int = 30,
        tasks: Tasks | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.window = Window(height)
        self.tabs = Tabs()
        self.tasks = Tasks() if tasks is None else tasks
        named = {} if labels is None else labels
        self.feed = ConversationFeed(
            self.window.conversation,
            self.tabs,
            self.tasks,
            Steps(),
            self.window.clock,
            lambda role: named.get(role, role),
        )

    @property
    def conversation(self) -> Conversation[Log]:
        return self.window.conversation

    @property
    def shown(self) -> Log:
        """The log shown."""
        return self.window.shown

    def events(self, *events: Event) -> None:
        """Have the conversation take in ``events``, as the app routes them."""
        for event in events:
            self.tabs.track(event)
            # A snapshot's roles are known only once it replaces the conversation.
            if not isinstance(event, Snapshot):
                self.conversation.track(event, self.tabs.roles)
            if in_group(event, FEED_EVENTS):
                self.feed.render(event)
        self.window.settle()

    def prompt(self, text: str) -> None:
        """Show ``text`` as the user's prompt to the planner."""
        self.feed.append(text, "prompt", PLANNER)
        self.window.settle()

    def show_only(self, key: str | None, label: str = "") -> None:
        """Show the tab keyed ``key``, labelled ``label``, as switching tabs does."""
        self.conversation.show_only(key, label)
        self.window.settle()

    def activate(self) -> None:
        """Activate the shown log, as enter on the conversation does."""
        self.shown.entry_log.start_at_latest()
        self.shown.has_focus = True

    def choose(self) -> None:
        """Press enter on the cursor's entry in the active log."""
        log = self.shown
        assert log.highlighted is not None
        log.entry_log.choose(log.highlighted)
        self.window.settle()

    def move(self, row: int | None) -> None:
        """Move the shown log's cursor to option ``row``."""
        self.shown.move(row)
        self.window.settle()

    def escape(self) -> None:
        """Press escape in the active log: it stops inspecting, else leaves list mode."""
        log = self.shown
        if not log.entry_log.stop_inspecting():
            log.has_focus = False
            log.entry_log.blurred(False)
        self.window.settle()

    def release(self) -> None:
        """Let go of the shown log's cursor, as escape with nothing active does."""
        self.conversation.release_cursor()
        self.window.settle()

    def toggle_thinking(self) -> None:
        self.conversation.toggle_thinking()
        self.window.settle()
