"""Structured messages exchanged between the frontend and the backend.

Commands flow from the frontend to the backend; events flow back. Both are
discriminated unions on ``type`` so they can be serialized to JSON and sent
across a process boundary without changes.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


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


class SubmitPrompt(_Message):
    """Ask the agent to start a turn with the given user input."""

    type: Literal["submit_prompt"] = "submit_prompt"
    text: str = Field(min_length=1)


class CancelTurn(_Message):
    """Ask the agent to stop the turn in progress, if any."""

    type: Literal["cancel_turn"] = "cancel_turn"


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

    Tasks added while it runs join in. Like a turn, this is rejected while
    anything runs; ``CancelTurn`` stops the task in progress and the run.
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


class SetPlanning(_Message):
    """Send prompts to the planner (``on``) or the plain conversation."""

    type: Literal["set_planning"] = "set_planning"
    on: bool


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
    | SetPlanning
    | CancelAgent
    | DecideProposal
    | ConfigureChecks,
    Field(discriminator="type"),
]


# State the backend owns and reports whole, so the frontend only renders it.

TurnEndReason = Literal["completed", "cancelled", "max_steps", "error"]

TaskStatus = (
    Literal["draft", "pending", "running", "verifying", "unverified", "gave_up"] | TurnEndReason
)
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


# Turn events carry ``agent_id`` when an agent runs the turn, and none for the conversation.


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
    type: Literal["turn_finished"] = "turn_finished"
    turn_id: str
    agent_id: str | None = None
    reason: TurnEndReason


class BusyChanged(_Message):
    """Whether a turn or a task run is in progress; new work is rejected while busy."""

    type: Literal["busy_changed"] = "busy_changed"
    busy: bool


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

    Not sent when no steps are configured.
    """

    type: Literal["verification_changed"] = "verification_changed"
    steps: tuple[StepState, ...]


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


class PlanningChanged(_Message):
    """Whether prompts now go to the planner; sent after every change."""

    type: Literal["planning_changed"] = "planning_changed"
    on: bool


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
    | PlanningChanged
    | ReviewStatus
    | ChecksProposed,
    Field(discriminator="type"),
]

command_adapter: TypeAdapter[Command] = TypeAdapter(Command)
event_adapter: TypeAdapter[Event] = TypeAdapter(Event)
