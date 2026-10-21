"""Fakes and helpers for driving the TUI: a scripted session, the events fed it, and
what the app shows."""

import asyncio
from collections.abc import AsyncIterator, Callable

from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from textual.pilot import Pilot
from textual.widgets import OptionList, Tab

from orbit.protocol import (
    PLANNER_LANE,
    WORK_LANE,
    AgentStarted,
    AssistantMessage,
    Attach,
    Command,
    CommandAccepted,
    CommandEnvelope,
    Event,
    EventEnvelope,
    Hello,
    LinkState,
    OpenRequest,
    ReviewStatus,
    RunningAgent,
    SessionSummary,
    Settings,
    SettingsChanged,
    Snapshot,
    StepState,
    TasksChanged,
    TaskSpec,
    TaskState,
    TaskStatus,
    ToolCallFinished,
    ToolCallStarted,
    Usage,
)
from orbit.tui import OrbitApp
from orbit.tui.entries import GutteredEntry
from orbit.tui.tabs import ConversationTabs
from orbit.tui.task_editor import TaskEditorScreen
from orbit.tui.tasks import TaskList
from orbit.tui.transcript import Transcript
from orbit.tui.transcript_log import TranscriptLog
from orbit.tui.view.common import EntryKind
from orbit.tui.widgets import PickerScreen


class FakeSession:
    """Stands in for the backend: records commands, and the envelopes they came in, and
    replays scripted events in envelopes; an exception queued fails the stream. ``sent`` has
    every envelope; ``envelopes`` and ``commands`` leave out the Hello opening the stream, which
    raises ``hello_error`` when one is set, as every other command raises ``send_error``, and
    the first Attach, which attaches the app as it opens."""

    def __init__(self) -> None:
        self.sent: list[CommandEnvelope] = []
        self.envelopes: list[CommandEnvelope] = []
        self.commands: list[Command] = []
        self.queue: asyncio.Queue[Event | Exception] = asyncio.Queue()
        self.closed = False
        self.hello_error: Exception | None = None
        self.send_error: Exception | None = None
        self.attached = False  # Whether the first Attach was sent.

    async def send(self, envelope: CommandEnvelope) -> None:
        self.sent.append(envelope)
        if isinstance(envelope.payload, Hello):
            if self.hello_error is not None:
                raise self.hello_error
            return
        if isinstance(envelope.payload, Attach) and not self.attached:
            self.attached = True
            return
        self.envelopes.append(envelope)
        self.commands.append(envelope.payload)
        if self.send_error is not None:
            raise self.send_error

    async def events(self) -> AsyncIterator[EventEnvelope]:
        seq = 0
        while True:
            event = await self.queue.get()
            if isinstance(event, Exception):
                raise event
            seq += 1
            yield EventEnvelope(seq=seq, payload=event)

    async def close(self) -> None:
        self.closed = True


CHOICES = ("opus", "sonnet")
"""The models the settings offer."""


async def show_settings(session: FakeSession, pilot: Pilot[None], settings: Settings) -> None:
    """Have ``session`` report ``settings``, offering :data:`CHOICES`, and let the app show them."""
    await session.queue.put(SettingsChanged(settings=settings, model_choices=CHOICES))
    await pilot.pause()


def picker(app: OrbitApp) -> tuple[str, list[str], int | None]:
    """The open picker's title, option labels and highlighted index."""
    screen = app.screen
    assert isinstance(screen, PickerScreen)
    options = screen.query_one(OptionList)
    labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
    return screen.title_text, labels, options.highlighted


def selected(app: OrbitApp) -> list[str | None]:
    """The ids of the selected windows."""
    return [w.id for w in app.query(".selected")]


async def feed_events(session: FakeSession, pilot: Pilot[None], *events: Event) -> None:
    """Have ``session`` send ``events`` and let the app take them in."""
    for event in events:
        await session.queue.put(event)
    await pilot.pause()


async def accept_last(session: FakeSession, pilot: Pilot[None]) -> None:
    """Have ``session`` accept the last command sent, and let the app take that in."""
    await feed_events(session, pilot, CommandAccepted(command_id=session.envelopes[-1].command_id))


PINNED = ["planner", "coder", "reviewer", "super-reviewer", "configurator"]
STRIP = ["All", *PINNED]


def opened(summary: SessionSummary) -> Snapshot:
    """A snapshot of ``summary``'s session opened with nothing in it, as a switch sends."""
    return Snapshot(
        session=summary,
        settings=Settings(),
        model_choices=(),
        busy={},
        tasks=(),
        steps=(),
        review=None,
        proposals=(),
        agents=(),
    )


def tab_strip(app: OrbitApp) -> list[str]:
    """The labels of the conversation tabs, in order."""
    return [tab.label_text for tab in app.query_one(ConversationTabs).query(Tab)]


def shown(app: OrbitApp) -> list[str]:
    """The texts of the entries on the tab being shown."""
    transcript = app.query_one(Transcript)
    return [entry.text for entry in transcript.entries(transcript.only)]


def entries(app: OrbitApp) -> list[tuple[str, EntryKind]]:
    """Every entry, with its kind, as the All tab has them."""
    return [(entry.text, entry.kind) for entry in app.query_one(Transcript).entries(None)]


def started(agent_id: str, role: str = "coder") -> AgentStarted:
    """``agent_id`` starting as ``role`` on task 1."""
    return AgentStarted(agent_id=agent_id, role=role, task_id="1")


def said(agent_id: str, text: str) -> AssistantMessage:
    """``agent_id`` saying ``text``."""
    return AssistantMessage(turn_id="t", text=text, agent_id=agent_id)


def active_tab(app: OrbitApp) -> str:
    """The label of the active conversation tab."""
    tab = app.query_one(ConversationTabs).active_tab
    assert tab is not None
    return tab.label_text


def task(task_id: str, role: str = "coder") -> TaskState:
    """A pending task ``task_id`` for ``role``."""
    return TaskState(id=task_id, task=TaskSpec(prompt=f"do {task_id}", role=role))


def rows(app: OrbitApp) -> list[str]:
    """The task list's rows."""
    options = app.query_one(TaskList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


def editor(app: OrbitApp) -> TaskEditorScreen:
    """The open task editor."""
    screen = app.screen
    assert isinstance(screen, TaskEditorScreen)
    return screen


def board(*prompts: str, status: TaskStatus = "pending") -> TasksChanged:
    """A board whose tasks have ids "1", "2", … and the given prompts."""
    return TasksChanged(
        tasks=tuple(
            TaskState(id=str(i), task=TaskSpec(prompt=p), status=status)
            for i, p in enumerate(prompts, 1)
        )
    )


async def open_task_list(pilot: Pilot[None]) -> None:
    """From the default selection (prompt), go to the task list and activate it."""
    await pilot.press("l", "k", "enter")


# Wraps in any window a test opens. At 40 columns a row beside the gutter holds two words but
# would hold three if wrapped at the full width, so cropping a row would lose text.
LONG = " ".join(["gutter"] * 20)


def visible_log(app: OrbitApp) -> TranscriptLog:
    return app.query_one("Transcript > TranscriptLog.-shown", TranscriptLog)


def entry(log: TranscriptLog, index: int) -> GutteredEntry:
    """The entry at ``index`` as the log draws it."""
    prompt = log.get_option_at_index(index).prompt
    assert isinstance(prompt, GutteredEntry)
    return prompt


def prompts(app: OrbitApp) -> list[GutteredEntry]:
    log = visible_log(app)
    return [entry(log, index) for index in range(log.option_count)]


def labels(app: OrbitApp) -> list[str]:
    """The shown log's entries as they're labelled, without their gutter."""
    return [prompt.label.plain for prompt in prompts(app)]


def label_style(prompt: GutteredEntry) -> Style:
    """The style of an entry's label."""
    return prompt.label.get_style_at_offset(Console(), 0)


async def scrolled_to_end(pilot: Pilot[None], log: TranscriptLog) -> bool:
    """Whether ``log`` reaches its end, waiting out a scroll that lags a refresh."""
    for _ in range(100):
        if log.scroll_y == log.max_scroll_y:
            return True
        await pilot.pause()
    return False


def entry_lines(log: TranscriptLog, index: int) -> list[list[Segment]]:
    """The rows of the entry at ``index`` as drawn at the log's width, gutter and all."""
    console = Console(width=log.size.width)
    return console.render_lines(entry(log, index), console.options, pad=False)


def entry_rows(log: TranscriptLog, index: int) -> list[str]:
    """The text of the rows of the entry at ``index``, gutter and all."""
    return ["".join(segment.text for segment in line) for line in entry_lines(log, index)]


def call(agent_id: str, call_id: str, path: str = "a.py") -> ToolCallStarted:
    return ToolCallStarted(
        turn_id="t", call_id=call_id, name="Read", arguments={"file_path": path}, agent_id=agent_id
    )


def result(agent_id: str, call_id: str, output: str, is_error: bool = False) -> ToolCallFinished:
    return ToolCallFinished(
        turn_id="t", call_id=call_id, output=output, is_error=is_error, agent_id=agent_id
    )


class FakeLink:
    """A link whose states a test moves it to."""

    def __init__(self) -> None:
        self.watchers: list[Callable[[LinkState], None]] = []

    def watch(self, changed: Callable[[LinkState], None]) -> None:
        self.watchers.append(changed)

    async def move(self, pilot: Pilot[None], state: LinkState) -> None:
        for changed in self.watchers:
            changed(state)
        await pilot.pause()


TASKS = (
    TaskState(id="1", task=TaskSpec(prompt="Add a flag."), status="running"),
    TaskState(id="2", task=TaskSpec(prompt="Document it.")),
)


CODER = RunningAgent(
    agent_id="agent-3",
    role="coder",
    task_id="1",
    open_requests=(OpenRequest(request_id="r1", tool="propose_checks"),),
)


SNAPSHOT = Snapshot(
    session=SessionSummary(
        id="s1", name="auth rework", usage=Usage(input_tokens=2_000, cost_usd=2.0)
    ),
    settings=Settings(model="opus"),
    model_choices=("opus", "sonnet"),
    busy={WORK_LANE: True, PLANNER_LANE: True},
    tasks=TASKS,
    steps=(StepState(name="Unit tests", status="running"),),
    steps_title="Startup check",
    review=ReviewStatus(paused=True, open_batches=0),
    proposals=(),
    agents=(CODER,),
)
