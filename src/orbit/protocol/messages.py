"""Structured messages exchanged between the frontend and the backend.

Commands flow from the frontend to the backend; events flow back. Both are
discriminated unions on ``type`` so they can be serialized to JSON and sent
across a process boundary without changes.
"""

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter


class _Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


ToolAccess = Literal["off", "restricted", "full"]
"""How much the agent may use tools.

- ``off``: no tools at all.
- ``restricted``: anything that would need approval is refused; read-only actions still work.
- ``full``: every tool call is approved automatically.
"""


class Settings(_Message):
    """Per-session agent configuration. ``model=None`` means the backend's default."""

    model: str | None = None
    tool_access: ToolAccess = "restricted"


# Commands: frontend -> backend

PROTOCOL_VERSION = 1
"""The protocol's major version, which a ``Hello`` must name to be welcomed."""

WORK_LANE = "work"
PLANNER_LANE = "planner"
"""The lanes' names: task runs in the work lane, the planner's turns in its own."""

SUPER_REVIEW = "super-review"
CONFIGURE_CHECKS = "configure-checks"
"""The built-in triggers' names: a super review now, and a configurator task to set up checks."""

PLANNER = "planner"
CODER = "coder"
REVIEWER = "reviewer"
SUPER_REVIEWER = "super-reviewer"
CONFIGURATOR = "configurator"
"""The built-in roles' names."""

BUILT_IN_ROLE_NAMES = (PLANNER, CODER, REVIEWER, SUPER_REVIEWER, CONFIGURATOR)
"""The built-in roles' names, in the order the TUI's strip shows their tabs."""


BusyLane = str
"""Where work runs, by name: task runs in ``work``; the planner's turns in ``planner``."""


class Hello(_Message):
    """Open the stream as ``client``, speaking ``protocol_version`` of the protocol; it must
    come before any other command. ``since`` is the last ``seq`` the client saw, if any; it's
    accepted and ignored for now. Answered with ``Welcome``, or refused."""

    type: Literal["hello"] = "hello"
    client: str
    protocol_version: int
    since: int | None = None


class SendMessage(_Message):
    """Send ``text`` to the conversational agent ``agent``, which answers in its own lane."""

    type: Literal["send_message"] = "send_message"
    agent: str = Field(min_length=1)
    text: str = Field(min_length=1)


class CancelTurn(_Message):
    """Stop the work in ``lane``: the planner's turn, or the task run."""

    type: Literal["cancel_turn"] = "cancel_turn"
    lane: BusyLane = WORK_LANE


class UpdateSettings(_Message):
    """Replace the session settings; they apply from the next turn."""

    type: Literal["update_settings"] = "update_settings"
    settings: Settings


class TaskSpec(_Message):
    """One queued prompt, run in its own fresh context with its own settings.

    ``role`` names the kind of agent that takes it, such as ``coder`` or ``reviewer``.
    """

    prompt: str = Field(min_length=1)
    settings: Settings = Settings()
    role: str = Field(default=CODER, min_length=1)


class AddTask(_Message):
    """Append ``task`` to the backend's task list."""

    type: Literal["add_task"] = "add_task"
    task: TaskSpec


class EditTask(_Message):
    """Replace task ``task_id`` with ``task`` and make it pending again.

    Rejected while that task runs.
    """

    type: Literal["edit_task"] = "edit_task"
    task_id: str
    task: TaskSpec


class RunTasks(_Message):
    """Run the task list: every task that hasn't finished, in order, each in a new context.

    Tasks added while it runs join in. Rejected while a run is in progress (the planner's
    turns don't count); ``CancelTurn`` stops the task in progress and the run.
    """

    type: Literal["run_tasks"] = "run_tasks"


class AcceptTask(_Message):
    """Make draft task ``task_id`` (one the planner proposed) a real task, to be run."""

    type: Literal["accept_task"] = "accept_task"
    task_id: str


class RemoveTask(_Message):
    """Take task ``task_id`` off the list; rejected while it runs."""

    type: Literal["remove_task"] = "remove_task"
    task_id: str


class CancelAgent(_Message):
    """Stop agent ``agent_id``'s work: a review on its own, or the run (or planner turn) it's in."""

    type: Literal["cancel_agent"] = "cancel_agent"
    agent_id: str


class FireTrigger(_Message):
    """Fire the manual trigger named ``trigger`` with ``input`` as its template values."""

    type: Literal["fire_trigger"] = "fire_trigger"
    trigger: str = Field(min_length=1)
    input: dict[str, str] = {}


ProposalAction = Literal["accept", "revise", "end"]
"""What the user made of proposed checks: take them, ask for changes, or end the task."""


class DecideProposal(_Message):
    """The user's decision on the checks proposed for task ``task_id``; ``feedback`` says what
    to change when the ``action`` is ``revise``."""

    type: Literal["decide_proposal"] = "decide_proposal"
    task_id: str
    action: ProposalAction
    feedback: str = ""


class ListSessions(_Message):
    """Ask for the saved sessions, answered with ``SessionsListed``."""

    type: Literal["list_sessions"] = "list_sessions"


class OpenSession(_Message):
    """Switch to saved session ``session_id``: its tasks, review state, planner conversation and
    settings replace the open session's, and nothing runs until asked. Refused while tasks, a
    review or the planner run."""

    type: Literal["open_session"] = "open_session"
    session_id: str


class NewSession(_Message):
    """Switch to a new, empty session ``name``d so, or by the time it started if ``name`` is
    none; refused as ``OpenSession`` is."""

    type: Literal["new_session"] = "new_session"
    name: str | None = None


class RenameSession(_Message):
    """Name the session ``session_id`` ``name``; refused for an empty name, or a session that's
    unreadable or open in another orbit. The sessions are listed again after."""

    type: Literal["rename_session"] = "rename_session"
    session_id: str
    name: str


class DeleteSession(_Message):
    """Delete the saved session ``session_id``; refused for the open session, or one open in
    another orbit. The sessions are listed again after."""

    type: Literal["delete_session"] = "delete_session"
    session_id: str


class ResetConversation(_Message):
    """End the conversation of the agent ``agent``, so its next message starts a fresh one, and
    re-read its role from the project's config. Refused while the agent answers."""

    type: Literal["reset_conversation"] = "reset_conversation"
    agent: str = Field(min_length=1)


Command = Annotated[
    Hello
    | SendMessage
    | CancelTurn
    | UpdateSettings
    | AddTask
    | EditTask
    | RunTasks
    | FireTrigger
    | AcceptTask
    | RemoveTask
    | CancelAgent
    | DecideProposal
    | ListSessions
    | OpenSession
    | NewSession
    | RenameSession
    | DeleteSession
    | ResetConversation,
    Field(discriminator="type"),
]


class Usage(_Message):
    """What turns cost: tokens of each kind, dollars, model round trips and time taken.

    Usages add up, so a task's total is the sum of its turns'.
    """

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    turns: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)

    @property
    def tokens(self) -> int:
        """Every token, of every kind."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    @property
    def read_tokens(self) -> int:
        """The tokens read: the input, with what was cached and what was read from the cache."""
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    def __add__(self, other: "Usage") -> "Usage":
        mine, theirs = self.model_dump(), other.model_dump()
        return Usage(**{name: mine[name] + theirs[name] for name in mine})


# State the backend owns and reports whole, so the frontend only renders it.

TurnEndReason = Literal["completed", "cancelled", "max_steps", "error"]

TaskStatus = Literal[
    "draft",
    "pending",
    "running",
    "verifying",
    "completed",
    "unverified",
    "gave_up",
    "cancelled",
    "error",
]
"""Where a task stands.

``verifying``: its turn completed and the verification steps are checking it.
``unverified``: its turn completed but a verification step failed.
``gave_up``: the agent reported it couldn't do the task.
``draft``: the planner proposed it; it waits to be accepted.
"""

ACTIVE_STATUSES: frozenset[TaskStatus] = frozenset({"running", "verifying"})
"""Statuses of a task an agent is working on or orbit is verifying."""

TaskOrigin = str
"""Who queued a task: ``manual`` (the user), ``planner``, or a review's suggestions,
``review`` or ``super_review``."""

MANUAL_ORIGIN = "manual"
PLANNER_ORIGIN = "planner"
REVIEW_ORIGIN = "review"
SUPER_REVIEW_ORIGIN = "super_review"
"""The task origins' names: the user, the planner, a review and a super review."""


class TaskState(_Message):
    """One entry of the task list. ``id`` stays the same while the list changes around it.

    ``fixes`` is the id of the task this one was added to fix, if any; it comes right
    after that task. ``attempt`` counts the fixes asked of the agent working on it, out
    of ``max_attempts``; both are 0 until verification first fails. A task a review
    suggested belongs to that review's ``batch``, at review ``round`` (0 otherwise).
    ``usage`` adds up what every turn run for the task cost. A task that ended badly says why
    in ``reason``, one line such as ``Unit tests failed``, with the full text in
    ``reason_detail``; both are empty otherwise.
    """

    id: str
    task: TaskSpec
    status: TaskStatus = "pending"
    fixes: str | None = None
    origin: TaskOrigin = MANUAL_ORIGIN
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=0, ge=0)
    batch: str | None = None
    round: int = Field(default=0, ge=0)
    usage: Usage = Usage()
    reason: str = ""
    reason_detail: str = ""


class SessionSummary(_Message):
    """A saved session as the list of sessions shows it: how many of its tasks stand at each
    status in ``counts``, and what they cost together in ``usage``. ``current`` marks the
    session open here, ``locked`` one open in another orbit, and ``unreadable`` says why a
    session can't be opened, if it can't."""

    id: str
    name: str
    last_active: AwareDatetime | None = None
    counts: dict[TaskStatus, int] = {}
    usage: Usage = Usage()
    current: bool = False
    locked: bool = False
    unreadable: str | None = None


StepStatus = Literal["pending", "running", "passed", "failed", "cancelled", "skipped"]


class StepState(_Message):
    """One verification step and how it fared after the current task."""

    name: str
    status: StepStatus = "pending"


# Events: backend -> frontend


class SettingsChanged(_Message):
    """Current settings, sent on connect and after every change."""

    type: Literal["settings_changed"] = "settings_changed"
    settings: Settings
    model_choices: tuple[str, ...] = ()


# Turn events carry ``agent_id`` when an agent runs the turn, and none when orbit ends it itself.


class TurnStarted(_Message):
    type: Literal["turn_started"] = "turn_started"
    turn_id: str
    agent_id: str | None = None


class AssistantMessage(_Message):
    type: Literal["assistant_message"] = "assistant_message"
    turn_id: str
    agent_id: str | None = None
    text: str


class ToolCallStarted(_Message):
    type: Literal["tool_call_started"] = "tool_call_started"
    turn_id: str
    agent_id: str | None = None
    call_id: str
    name: str
    arguments: dict[str, Any]


class ToolCallFinished(_Message):
    type: Literal["tool_call_finished"] = "tool_call_finished"
    turn_id: str
    agent_id: str | None = None
    call_id: str
    output: str
    is_error: bool = False


class TurnFinished(_Message):
    """The turn ended for ``reason``; ``usage`` says what it cost, when the runner knows."""

    type: Literal["turn_finished"] = "turn_finished"
    turn_id: str
    agent_id: str | None = None
    reason: TurnEndReason
    usage: Usage | None = None


class BusyChanged(_Message):
    """Whether ``lane`` has work in progress; new work for that lane is rejected while busy."""

    type: Literal["busy_changed"] = "busy_changed"
    busy: bool
    lane: BusyLane = WORK_LANE


class TasksChanged(_Message):
    """The whole task list, sent after every change to it."""

    type: Literal["tasks_changed"] = "tasks_changed"
    tasks: tuple[TaskState, ...]


class TaskStarted(_Message):
    """Task ``task_id`` begins; its turn's events follow."""

    type: Literal["task_started"] = "task_started"
    task_id: str


class VerificationChanged(_Message):
    """The verification steps and their progress, sent on connect and after every change.

    Not sent when no steps are configured. ``title`` names what the steps are checking when
    it isn't the current task, such as the startup check; the panel shows it as a heading.
    """

    type: Literal["verification_changed"] = "verification_changed"
    steps: tuple[StepState, ...]
    title: str = ""


STARTUP_TASK = "startup"
"""The task the startup check's ``AgentStarted`` names; no task on the list has this id."""

PLAN_TASK = "plan"
"""The task the planner's ``AgentStarted`` names; it works on no task of the list."""


class StartupChecked(_Message):
    """The verification steps ran once on the base commit at startup. Unless they all
    ``passed``, ``step`` failed with ``output`` (its end), and a draft task to fix it was added."""

    type: Literal["startup_checked"] = "startup_checked"
    passed: bool
    step: str = ""
    output: str = ""


class VerificationStarted(_Message):
    """Step ``step`` (an index into the steps) begins checking task ``task_id``."""

    type: Literal["verification_started"] = "verification_started"
    task_id: str
    step: int


SkipReason = Literal["tools_off", "no_changes", "unchanged", "no_steps"]
"""Why a task wasn't verified: it ran with tools off, it changed no files, its agent stopped
without changing anything since its last failed verification, or no steps are configured."""


class VerificationSkipped(_Message):
    """Task ``task_id`` completed but has nothing to verify, so no step runs."""

    type: Literal["verification_skipped"] = "verification_skipped"
    task_id: str
    reason: SkipReason


class VerificationFinished(_Message):
    """Step ``step`` (an index into the steps) finished checking task ``task_id``."""

    type: Literal["verification_finished"] = "verification_finished"
    task_id: str
    step: int
    passed: bool


class ErrorOccurred(_Message):
    """An error with no command behind it, of the kind ``code`` names (one of the error codes,
    such as :data:`AGENT_FAILED`, or a refusal code where it fits), and ``message`` says what."""

    type: Literal["error_occurred"] = "error_occurred"
    turn_id: str | None = None
    agent_id: str | None = None
    code: str
    message: str


class CommandAccepted(_Message):
    """Command ``command_id`` was handled; the events its handling published came before."""

    type: Literal["command_accepted"] = "command_accepted"
    command_id: str


class Welcome(_Message):
    """``Hello`` ``command_id`` was received, in place of its ``CommandAccepted``: the backend,
    orbit ``server_version``, publishes its events from here on."""

    type: Literal["welcome"] = "welcome"
    command_id: str
    server_version: str


NOT_CONFIGURED = "not_configured"
"""A refusal's code: what the command needs isn't set up, such as agents or verification."""

UNKNOWN = "unknown"
"""A refusal's code: the command names something there's none of, such as a task or a lane."""

BUSY = "busy"
"""A refusal's code: what the command acts on is running, so it can't be done now."""

INVALID = "invalid"
"""A refusal's code: the command can't be done as given, such as input to a trigger that takes
none."""

LOCKED = "locked"
"""A refusal's code: another orbit holds what the command acts on."""

WRONG_SESSION = "wrong_session"
"""A refusal's code: the command is for a session that isn't open."""

INCOMPATIBLE = "incompatible"
"""A refusal's code: a ``Hello`` named a protocol version this orbit doesn't speak; nothing more
is sent on that stream."""

AGENT_FAILED = "agent_failed"
"""An error's code: an agent's run or turn failed, or it exited without doing its task."""

TIMED_OUT = "timed_out"
"""An error's code: an agent ran out of time."""

GIT_FAILED = "git_failed"
"""An error's code: a git command or a worktree failed."""

SAVE_FAILED = "save_failed"
"""An error's code: saving the session failed."""

CHECKS_EXHAUSTED = "checks_exhausted"
"""An error's code: a check still failed after every fix attempt."""

INTERNAL = "internal"
"""An error's code: orbit itself failed unexpectedly."""


class CommandRefused(_Message):
    """Command ``command_id`` was refused, for the reason ``code`` names (one of the refusal
    codes, such as :data:`UNKNOWN`), and ``message`` says why; it changed nothing."""

    type: Literal["command_refused"] = "command_refused"
    command_id: str
    code: str
    message: str


MergeResult = Literal["merged", "rebased", "conflict", "blocked", "detached"]
"""What came of merging a task's commit into the base branch.

``merged``: the base now has it. ``rebased``: the base had moved, so it was replayed on top and
must be verified again. ``conflict``: it couldn't be replayed. ``blocked``: the base's checkout
has changes in the way. ``detached``: there's no base branch to merge into.
"""


class TaskMerged(_Message):
    """Task ``task_id``'s work was committed as ``commit``, and this came of merging it;
    ``detail`` says why when it wasn't merged."""

    type: Literal["task_merged"] = "task_merged"
    task_id: str
    result: MergeResult
    detail: str = ""
    commit: str = ""


class ReviewStatus(_Message):
    """Whether review is paused, and how many review batches are still open; sent on changes."""

    type: Literal["review_status"] = "review_status"
    paused: bool
    open_batches: int


class Notice(_Message):
    """Something worth the user's attention that isn't an error."""

    type: Literal["notice"] = "notice"
    message: str


AgentExitReason = Literal["finished", "crashed", "killed"]
"""How an agent's process ended: on its own, with a failure, or stopped by orbit."""


class AgentStarted(_Message):
    """Agent ``agent_id`` was started in the ``role`` to work on task ``task_id``."""

    type: Literal["agent_started"] = "agent_started"
    agent_id: str
    role: str
    task_id: str


class AgentRequest(_Message):
    """Agent ``agent_id`` called one of orbit's tools; an ``AgentReply`` with the same
    ``request_id`` follows."""

    type: Literal["agent_request"] = "agent_request"
    agent_id: str
    request_id: str
    tool: str
    arguments: dict[str, Any]


class AgentReply(_Message):
    """Orbit's answer to request ``request_id``."""

    type: Literal["agent_reply"] = "agent_reply"
    agent_id: str
    request_id: str
    text: str
    is_error: bool = False


class ProgressNoted(_Message):
    """A short status note an agent reported while working on task ``task_id``."""

    type: Literal["progress_noted"] = "progress_noted"
    agent_id: str
    task_id: str
    note: str


class TaskGaveUp(_Message):
    """The agent working on task ``task_id`` reported it can't do it, and why."""

    type: Literal["task_gave_up"] = "task_gave_up"
    agent_id: str
    task_id: str
    reason: str


class AgentExited(_Message):
    """Agent ``agent_id``'s process ended."""

    type: Literal["agent_exited"] = "agent_exited"
    agent_id: str
    reason: AgentExitReason


class ProposedStep(_Message):
    """A verification step an agent proposed: its ``name`` and ``command``."""

    name: str
    command: tuple[str, ...]


class ProposedSetup(_Message):
    """The setup an agent proposed: its ``command``, and the files whose changes rerun it."""

    command: tuple[str, ...]
    watch: tuple[str, ...] = ()


class ChecksProposed(_Message):
    """Agent ``agent_id`` proposed these checks for task ``task_id``, and they all passed on
    the current code; the user decides on them with ``DecideProposal``."""

    type: Literal["checks_proposed"] = "checks_proposed"
    agent_id: str
    task_id: str
    steps: tuple[ProposedStep, ...]
    setup: ProposedSetup | None = None


class UsageLimit(_Message):
    """How much of a subscription usage window, such as ``Session`` or ``Week``, is used, and
    when it resets (timezone-aware), if known."""

    label: str
    percent: float = Field(ge=0, le=100)
    resets_at: AwareDatetime | None = None


UsageStatus = Literal["ok", "rate_limited", "unavailable"]
"""Whether the latest usage fetch worked, was rate limited, or couldn't be made."""


class UsageChanged(_Message):
    """The subscription's usage limits, sent after every fetch.

    Unless the ``status`` is ``ok``, ``limits`` are the last good ones; ``detail`` says why
    usage is unavailable, and ``retry_at`` when a rate-limited fetch is tried again.
    """

    type: Literal["usage_changed"] = "usage_changed"
    limits: tuple[UsageLimit, ...] = ()
    status: UsageStatus = "ok"
    detail: str = ""
    retry_at: AwareDatetime | None = None


ListingReason = Literal["startup", "asked", "refreshed"]
"""Why sessions are listed: for the user to pick one at startup, in answer to
``ListSessions``, or because a rename or delete changed them."""


class SessionsListed(_Message):
    """The saved sessions, the most recently active first; those that can't be read come last.

    The ``reason`` says what the listing answers.
    """

    type: Literal["sessions_listed"] = "sessions_listed"
    sessions: tuple[SessionSummary, ...]
    reason: ListingReason


class ConversationReset(_Message):
    """The conversation of the agent ``agent`` ended at the user's ``ResetConversation``; its
    next message starts a fresh one."""

    type: Literal["conversation_reset"] = "conversation_reset"
    agent: str


class OpenRequest(_Message):
    """A call ``request_id`` to orbit's ``tool`` that an agent made and orbit hasn't answered."""

    request_id: str
    tool: str


class RunningAgent(_Message):
    """Agent ``agent_id``, running in the ``role`` on task ``task_id``, with its
    ``open_requests``, oldest first."""

    agent_id: str
    role: str
    task_id: str
    open_requests: tuple[OpenRequest, ...] = ()


class Snapshot(_Message):
    """The open session as it stands, sent after ``Welcome`` and after every switch; its
    envelope's ``seq`` is the point it's current to.

    It holds the ``session``'s summary, its ``settings`` and ``model_choices``, whether each
    lane is ``busy``, its ``tasks``, the verification ``steps`` and the ``steps_title`` they
    show under, the ``review`` status (none when reviews are off), the checks ``proposals``
    waiting for a decision, and the ``agents`` running.
    """

    type: Literal["snapshot"] = "snapshot"
    session: SessionSummary
    settings: Settings
    model_choices: tuple[str, ...]
    busy: dict[BusyLane, bool]
    tasks: tuple[TaskState, ...]
    steps: tuple[StepState, ...]
    steps_title: str = ""
    review: ReviewStatus | None
    proposals: tuple[ChecksProposed, ...]
    agents: tuple[RunningAgent, ...]


Event = Annotated[
    SettingsChanged
    | TurnStarted
    | AssistantMessage
    | ToolCallStarted
    | ToolCallFinished
    | TurnFinished
    | BusyChanged
    | TasksChanged
    | TaskStarted
    | VerificationChanged
    | StartupChecked
    | VerificationStarted
    | VerificationSkipped
    | VerificationFinished
    | ErrorOccurred
    | AgentStarted
    | AgentRequest
    | AgentReply
    | ProgressNoted
    | TaskGaveUp
    | AgentExited
    | TaskMerged
    | Notice
    | ReviewStatus
    | ChecksProposed
    | UsageChanged
    | SessionsListed
    | ConversationReset
    | Snapshot
    | Welcome
    | CommandAccepted
    | CommandRefused,
    Field(discriminator="type"),
]

command_adapter: TypeAdapter[Command] = TypeAdapter(Command)
event_adapter: TypeAdapter[Event] = TypeAdapter(Event)
