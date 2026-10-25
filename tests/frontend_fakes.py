"""The app's frontend driven without Textual: a :class:`Frontend` over fake views holding the
widgets' real models, talking to a fake session, as the app wires them."""

from collections.abc import Awaitable, Callable

from orbit.protocol import (
    Attach,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Event,
    Hello,
    TaskSpec,
    UsageChanged,
    Watch,
)
from orbit.tracing import NoTelemetry, Telemetry
from orbit.tui.view.actions import carrying
from orbit.tui.view.agents import AgentsList
from orbit.tui.view.common import EntryKind
from orbit.tui.view.frontend import Frontend, Parts, Saved
from orbit.tui.view.opening import Opening
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.panels import VerificationSteps
from orbit.tui.view.prompt import PromptMarks
from orbit.tui.view.settings import Pickers
from orbit.tui.view.storage import StorageHalt
from orbit.tui.view.tab_strip import ALL_TAB_ID, OPENING, TabStrip
from orbit.tui.view.takeover import TakeoverOffer
from orbit.tui.view.task_editor import TaskChoices
from orbit.tui.view.tasks import TaskBoard
from tests.navigation_fakes import Modals, WorkspaceViews
from tests.transcript_fakes import Window
from tests.view_fakes import (
    AgentsView,
    HaltView,
    OfferView,
    PickerScreens,
    ProposalModal,
    StepsView,
    TaskView,
)

Message = Callable[[], None]

_NO_TELEMETRY = NoTelemetry()


class Wire:
    """The open session, as the frontend sends to it: every envelope sent, in turn. The Hello
    raises ``hello_error`` while one is set, as every other command raises ``send_error``."""

    def __init__(self) -> None:
        self.sent: list[CommandEnvelope] = []
        self.hello_error: Exception | None = None
        self.send_error: Exception | None = None

    async def send(self, envelope: CommandEnvelope) -> None:
        self.sent.append(envelope)
        error = self.hello_error if isinstance(envelope.payload, Hello) else self.send_error
        if error is not None:
            raise error

    @property
    def commands(self) -> list[Command]:
        """The commands sent but for the Hello."""
        return [e.payload for e in self.sent if not isinstance(e.payload, Hello)]

    @property
    def changes(self) -> list[Command]:
        """The commands sent but for the Hello, attaching and the streams pickers watch."""
        return [c for c in self.commands if not isinstance(c, Attach | Watch)]


class Prompt:
    """The prompt box: what's typed in it, and whether the user is typing."""

    def __init__(self) -> None:
        self.text = ""
        self.typing = False

    def clear(self) -> None:
        self.text = ""

    def normal_mode(self) -> None:
        self.typing = False

    def enter_insert_mode(self) -> None:
        self.typing = True


class AppShell:
    """What the app itself does for the frontend, as a test sees it: the warnings, questions
    and task editors it showed, the trigger keys' labels, the window marked selected, the
    windows entered and how often it took the focus away. Nothing has the focus but the
    prompt while the user types in it, or another window a test says has it."""

    def __init__(self, prompt: Prompt, window: Window) -> None:
        self._prompt = prompt
        self._window = window
        self.focused: str | None = None
        self.sidebar_shown = True
        self.warnings: list[str] = []
        self.questions: list[tuple[str, str, str, Callable[[], Awaitable[object]]]] = []
        self.editors: list[tuple[TaskSpec | None, TaskChoices, Saved]] = []
        self.labels: dict[str, str | None] = {}
        self.selected: set[str] = set()
        self.entered: list[str] = []
        self.unfocused = 0
        self.task_list_unfocused = 0

    @property
    def nothing_focused(self) -> bool:
        return self.focused is None and not self._prompt.typing

    @property
    def detail_open(self) -> bool:
        return self._window.pane.open

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def confirm(
        self, title: str, message: str, yes: str, then: Callable[[], Awaitable[object]]
    ) -> None:
        self.questions.append((title, message, yes, then))

    def edit_task(self, task: TaskSpec | None, choices: TaskChoices, saved: Saved) -> None:
        self.editors.append((task, choices, saved))

    def label_triggers(self, labels: dict[str, str | None]) -> None:
        self.labels = labels

    def mark_selected(self, window: str, selected: bool) -> None:
        if selected:
            self.selected.add(window)
        else:
            self.selected.discard(window)

    def enter(self, window: str) -> None:
        self.entered.append(window)

    def unfocus(self) -> None:
        self.unfocused += 1
        self.focused = None

    def unfocus_task_list(self) -> None:
        self.task_list_unfocused += 1
        if self.focused == "task-list":
            self.focused = None


class Strip:
    """The strip of conversation tabs: their labels by id, in order, and the active one. Like
    Textual's, activating another tab, and relabelling the active one, post messages, which
    reach the frontend only once the shell settles."""

    def __init__(self, shell: Shell) -> None:
        self._shell = shell
        self.labels = dict(OPENING)
        self.active = ""

    def activate(self, tab_id: str) -> None:
        assert tab_id in self.labels
        if tab_id == self.active:
            return
        self.active = tab_id
        label = self.labels[tab_id]
        self._shell.post(lambda: self._shell.frontend.tab_shown(tab_id, label))

    def open_tab(self, tab_id: str, label: str) -> None:
        assert tab_id not in self.labels
        self.labels[tab_id] = label

    def close_tab(self, tab_id: str) -> None:
        assert tab_id != self.active
        del self.labels[tab_id]

    def label_tab(self, tab_id: str, label: str) -> None:
        self.labels[tab_id] = label

    def relabelled(self, key: str | None, label: str) -> None:
        self._shell.post(lambda: self._shell.frontend.retitle(key, label))

    def id_of(self, label: str) -> str:
        """The id of the tab labelled ``label``."""
        return next(tab_id for tab_id, shown in self.labels.items() if shown == label)


class Marks:
    """The prompt's title and subtitle."""

    def __init__(self) -> None:
        self.title = ""
        self.subtitle = ""

    def show_title(self, title: str) -> None:
        self.title = title

    def show_subtitle(self, subtitle: str) -> None:
        self.subtitle = subtitle


class Line:
    """A status line: the status it shows, none while it's hidden."""

    def __init__(self) -> None:
        self.status: str | None = None

    def show(self, note: str | None) -> None:
        self.status = note


class Paging:
    """What shows above the conversation's entries: whether earlier ones are loading, or were
    trimmed."""

    def __init__(self) -> None:
        self.loading = False
        self.trimmed = False

    def show_loading(self, loading: bool) -> None:
        self.loading = loading

    def show_trimmed(self, trimmed: bool) -> None:
        self.trimmed = trimmed


class Usage:
    """The usage panel: the reports it showed."""

    def __init__(self) -> None:
        self.reports: list[UsageChanged] = []

    def show(self, event: UsageChanged) -> None:
        self.reports.append(event)


class Shell:
    """A frontend over fake views holding the widgets' real models, opening as ``opening``
    says, over a link if ``linked``, sending to a fake session, its actions traced with
    ``telemetry``, timed by ``now``. What the widgets and Textual do later, a test has done
    with :meth:`settle`."""

    def __init__(
        self,
        opening: Opening | None = None,
        linked: bool = False,
        telemetry: Telemetry = _NO_TELEMETRY,
    ) -> None:
        self.now = 0.0
        self.wire = Wire()
        self.window = Window()
        self.prompt = Prompt()
        self.app = AppShell(self.prompt, self.window)
        self.strip = Strip(self)
        self.marks = Marks()
        self.viewing, self.paused = Line(), Line()
        self.task_view, self.steps_view, self.agents_view = TaskView(), StepsView(), AgentsView()
        self.usage, self.paging = Usage(), Paging()
        self.modals = Modals(self.send)
        self.proposals, self.pickers = ProposalModal(), PickerScreens()
        self.halt_view, self.offer_view = HaltView(), OfferView()
        self._posted: list[Message] = []
        self.tabs = TabStrip(self.strip)
        self.tasks = TaskBoard(self.task_view, self.clock)
        self.steps = VerificationSteps(self.steps_view, self.clock)
        self.agents = AgentsList(self.agents_view)
        self.prompt_marks = PromptMarks(self.marks)
        self.parts = self._parts()
        send = carrying(self.wire.send, telemetry)
        self.frontend = Frontend(self.parts, send, opening or Opening(), linked)
        self._mount()

    def _parts(self) -> Parts:
        clock = self.clock
        return Parts(
            app=self.app,
            conversation=self.window.conversation,
            paging=self.paging,
            tabs=self.tabs,
            prompt=self.prompt,
            marks=self.prompt_marks,
            viewing=self.viewing,
            paused=self.paused,
            tasks=self.tasks,
            steps=self.steps,
            usage=self.usage,
            agents=self.agents,
            place=self.modals,
            workspaces=WorkspaceViews(self.modals),
            proposals=self.proposals,
            pickers=Pickers(self.pickers),
            halt=StorageHalt(self.halt_view, self.send),
            takeover=TakeoverOffer(self.offer_view, self.send),
            clock=clock,
        )

    def _mount(self) -> None:
        """Do what the app's widgets do once mounted."""
        self.tasks.start()
        self.agents.show()
        self.prompt_marks.show_busy(False)
        self.frontend.start()
        self.strip.activate(ALL_TAB_ID)  # Tabs make their first tab active.

    def clock(self) -> float:
        return self.now

    async def send(self, command: Command, awaited: Awaited | None = None) -> bool:
        return await self.frontend.send(command, awaited)

    def post(self, message: Message) -> None:
        """Post ``message``, which reaches the frontend once the shell settles."""
        self._posted.append(message)

    async def settle(self) -> None:
        """Deliver the messages posted, hand on what the modals closed with and take in what
        the conversation's logs said of the detail pane, as Textual does once idle."""
        while self._posted:
            self._posted.pop(0)()
        await self.modals.settle()
        self.window.settle()

    async def feed(self, *events: Event) -> None:
        """Have the frontend take ``events`` in as one batch, then settle."""
        self.frontend.show_batch(events)
        await self.frontend.listed(events)
        await self.settle()

    async def accept_last(self) -> None:
        """Have the session accept the last command sent."""
        await self.feed(CommandAccepted(command_id=self.wire.sent[-1].command_id))

    async def refuse(self, code: str, message: str, index: int = -1) -> CommandRefused:
        """Have the session refuse the command sent at ``index``, the last by default, with
        ``code`` saying ``message``; the refusal."""
        refusal = CommandRefused(
            command_id=self.wire.sent[index].command_id, code=code, message=message
        )
        await self.feed(refusal)
        return refusal

    async def submit(self, text: str) -> None:
        """Type ``text`` in the prompt, as the user does, and press enter."""
        self.prompt.text, self.prompt.typing = text, True
        await self.frontend.submit(self.prompt.text)
        await self.settle()

    async def click_tab(self, label: str) -> None:
        """Click the tab labelled ``label``, as the user does."""
        self.strip.activate(self.strip.id_of(label))
        await self.settle()

    async def cycle(self, step: int, times: int = 1) -> None:
        """Press tab, or shift+tab with a negative ``step``, ``times`` times."""
        for _ in range(times):
            self.frontend.cycle_tab(step)
            await self.settle()

    async def toggle_planner(self) -> None:
        """Press ctrl+t."""
        self.frontend.toggle_planner()
        await self.settle()

    def entries(self) -> list[tuple[str, EntryKind]]:
        """Every entry, with its kind, as the All tab has them."""
        return [(entry.text, entry.kind) for entry in self.window.conversation.entries(None)]

    def shown(self) -> list[str]:
        """The texts of the entries on the tab being shown."""
        conversation = self.window.conversation
        return [entry.text for entry in conversation.entries(conversation.only)]

    @property
    def tab_strip(self) -> list[str]:
        """The tabs' labels, in order."""
        return list(self.strip.labels.values())

    @property
    def active_tab(self) -> str:
        """The label of the active tab."""
        return self.strip.labels[self.strip.active]

    def tab_entries(self, key: str | None) -> list[str]:
        """The texts of the entries under the tab keyed ``key``."""
        return [entry.text for entry in self.window.conversation.entries(key)]
