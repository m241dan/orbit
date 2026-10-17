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


BusyLane = Literal["work", "planner"]
"""Where work runs: task runs in ``work``; the planner's turns in a lane of their own."""


class SubmitPrompt(_Message):
    """Ask the planner to start a turn with the given user input.

    The planner answers in its own lane, so it works while tasks run.
    """

    type: Literal["submit_prompt"] = "submit_prompt"
    text: str = Field(min_length=1)


class CancelTurn(_Message):
    """Stop the work in ``lane``: the planner's turn, or the task run."""

    type: Literal["cancel_turn"] = "cancel_turn"
    lane: BusyLane = "work"


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
    role: str = Field(default="coder", min_length=1)


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


class RequestSuperReview(_Message):
    """Ask for a super review of the whole codebase now, without waiting for its turn."""

    type: Literal["request_super_review"] = "request_super_review"


class ConfigureChecks(_Message):
    """Queue a configurator task at the front of the task list and run it now, told the steps
    and setup configured so far; refused while one is queued or running."""

    type: Literal["configure_checks"] = "configure_checks"


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


class ResetPlanner(_Message):
    """End the planner's conversation, so the next prompt starts a fresh one, and re-read its
    role from the project's config. Refused while the planner answers."""

    type: Literal["reset_planner"] = "reset_planner"


Command = Annotated[
    SubmitPrompt
    | CancelTurn
    | UpdateSettings
    | AddTask
    | EditTask
    | RunTasks
    | RequestSuperReview
    | AcceptTask
    | RemoveTask
    | CancelAgent
    | DecideProposal
    | ConfigureChecks
    | ListSessions
    | OpenSession
    | NewSession
    | RenameSession
    | DeleteSession
    | ResetPlanner,
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

TaskOrigin = Literal["manual", "planner", "review", "super_review"]
"""Who queued a task: the user, the planner, or a (super) review's suggestions."""


class TaskState(_Message):
    """One entry of the task list. ``id`` stays the same while the list changes around it.

    ``fixes`` is the id of the task this one was added to fix, if any; it comes right
    after that task. ``attempt`` counts the fixes asked of the agent working on it, out
    of ``max_attempts``; both are 0 until verification first fails. A task a review
    suggested belongs to that review's ``batch``, at review ``round`` (0 otherwise).
    ``usage`` adds up what every turn run for the task cost.
    """

    id: str
    task: TaskSpec
    status: TaskStatus = "pending"
    fixes: str | None = None
    origin: TaskOrigin = "manual"
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=0, ge=0)
    batch: str | None = None
    round: int = Field(default=0, ge=0)
    usage: Usage = Usage()


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
    lane: BusyLane = "work"


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
    type: Literal["error_occurred"] = "error_occurred"
    turn_id: str | None = None
    agent_id: str | None = None
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


class SessionsListed(_Message):
    """The saved sessions, the most recently active first; those that can't be read come last."""

    type: Literal["sessions_listed"] = "sessions_listed"
    sessions: tuple[SessionSummary, ...]


class SessionOpened(_Message):
    """The ``session`` now open, after ``OpenSession`` or ``NewSession``."""

    type: Literal["session_opened"] = "session_opened"
    session: SessionSummary


class PlannerReset(_Message):
    """The planner's conversation ended at the user's ``ResetPlanner``; the next prompt starts
    a fresh one."""

    type: Literal["planner_reset"] = "planner_reset"


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
    | SessionOpened
    | PlannerReset,
    Field(discriminator="type"),
]

command_adapter: TypeAdapter[Command] = TypeAdapter(Command)
event_adapter: TypeAdapter[Event] = TypeAdapter(Event)

PLANNER = "planner"
CODER = "coder"
REVIEWER = "reviewer"
SUPER_REVIEWER = "super-reviewer"
CONFIGURATOR = "configurator"
"""The built-in roles' names."""

BUILT_IN_ROLE_NAMES = (PLANNER, CODER, REVIEWER, SUPER_REVIEWER, CONFIGURATOR)
"""The built-in roles' names, in the order the TUI's strip shows their tabs."""
