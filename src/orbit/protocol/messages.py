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


class RunTasks(_Message):
    """Run ``tasks`` one after another, each in a new context.

    Like a turn, this is rejected while a turn is running. ``CancelTurn`` stops
    the task in progress and skips the rest.
    """

    type: Literal["run_tasks"] = "run_tasks"
    tasks: tuple[TaskSpec, ...] = Field(min_length=1)


Command = Annotated[
    SubmitPrompt | CancelTurn | UpdateSettings | RunTasks, Field(discriminator="type")
]


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


TurnEndReason = Literal["completed", "cancelled", "max_steps", "error"]


class TurnFinished(_Message):
    type: Literal["turn_finished"] = "turn_finished"
    turn_id: str
    reason: TurnEndReason


class TaskStarted(_Message):
    """Task ``index`` of the current ``RunTasks`` begins; its turn's events follow."""

    type: Literal["task_started"] = "task_started"
    index: int


class TaskFinished(_Message):
    """Task ``index`` is done.

    ``verified`` says whether the verification steps passed after it; ``None``
    when none ran (no steps configured, or the task's turn didn't complete).
    """

    type: Literal["task_finished"] = "task_finished"
    index: int
    reason: TurnEndReason
    verified: bool | None = None


class TaskInserted(_Message):
    """``task`` joins the running batch at ``index``; tasks from there on shift down one.

    Sent before the ``TaskFinished`` of the task it follows.
    """

    type: Literal["task_inserted"] = "task_inserted"
    index: int
    task: TaskSpec


class VerificationConfigured(_Message):
    """Names of the steps run after each task, sent on connect when there are any."""

    type: Literal["verification_configured"] = "verification_configured"
    steps: tuple[str, ...]


class VerificationStarted(_Message):
    """Step ``step`` (an index into the configured steps) begins checking task ``index``."""

    type: Literal["verification_started"] = "verification_started"
    index: int
    step: int


class VerificationFinished(_Message):
    type: Literal["verification_finished"] = "verification_finished"
    index: int
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
    | TaskStarted
    | TaskFinished
    | TaskInserted
    | VerificationConfigured
    | VerificationStarted
    | VerificationFinished
    | ErrorOccurred,
    Field(discriminator="type"),
]

command_adapter: TypeAdapter[Command] = TypeAdapter(Command)
event_adapter: TypeAdapter[Event] = TypeAdapter(Event)
