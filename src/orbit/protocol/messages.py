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
    """One queued prompt, run in its own fresh context with its own settings."""

    prompt: str = Field(min_length=1)
    settings: Settings = Settings()


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


Command = Annotated[
    SubmitPrompt | CancelTurn | UpdateSettings | AddTask | EditTask | RunTasks,
    Field(discriminator="type"),
]


# State the backend owns and reports whole, so the frontend only renders it.

TurnEndReason = Literal["completed", "cancelled", "max_steps", "error"]

TaskStatus = Literal["pending", "running", "verifying", "unverified"] | TurnEndReason
"""Where a task stands.

``verifying``: its turn completed and the verification steps are checking it.
``unverified``: its turn completed but a verification step failed.
"""


class TaskState(_Message):
    """One entry of the task list. ``id`` stays the same while the list changes around it."""

    id: str
    task: TaskSpec
    status: TaskStatus = "pending"


StepStatus = Literal["pending", "running", "passed", "failed", "cancelled"]


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


class TurnStarted(_Message):
    type: Literal["turn_started"] = "turn_started"
    turn_id: str


class AssistantMessage(_Message):
    type: Literal["assistant_message"] = "assistant_message"
    turn_id: str
    text: str


class ToolCallStarted(_Message):
    type: Literal["tool_call_started"] = "tool_call_started"
    turn_id: str
    call_id: str
    name: str
    arguments: dict[str, Any]


class ToolCallFinished(_Message):
    type: Literal["tool_call_finished"] = "tool_call_finished"
    turn_id: str
    call_id: str
    output: str
    is_error: bool = False


class TurnFinished(_Message):
    type: Literal["turn_finished"] = "turn_finished"
    turn_id: str
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


class VerificationFinished(_Message):
    """Step ``step`` (an index into the steps) finished checking task ``task_id``."""

    type: Literal["verification_finished"] = "verification_finished"
    task_id: str
    step: int
    passed: bool


class ErrorOccurred(_Message):
    type: Literal["error_occurred"] = "error_occurred"
    turn_id: str | None = None
    message: str


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
    | VerificationFinished
    | ErrorOccurred,
    Field(discriminator="type"),
]

command_adapter: TypeAdapter[Command] = TypeAdapter(Command)
event_adapter: TypeAdapter[Event] = TypeAdapter(Event)
