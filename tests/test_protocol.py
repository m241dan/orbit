import pytest
from pydantic import ValidationError

from orbit.protocol import (
    AddTask,
    BusyChanged,
    EditTask,
    RunTasks,
    Settings,
    SettingsChanged,
    StepState,
    SubmitPrompt,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskState,
    ToolCallStarted,
    UpdateSettings,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
    command_adapter,
    event_adapter,
)


def test_event_json_round_trip() -> None:
    event = ToolCallStarted(turn_id="t1", call_id="c1", name="read", arguments={"path": "x"})
    assert event_adapter.validate_json(event.model_dump_json()) == event


def test_command_dispatches_on_type() -> None:
    command = command_adapter.validate_json('{"type": "submit_prompt", "text": "hi"}')
    assert command == SubmitPrompt(text="hi")


def test_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        command_adapter.validate_python({"type": "submit_prompt", "text": "hi", "extra": 1})


def test_rejects_empty_prompt() -> None:
    with pytest.raises(ValidationError):
        SubmitPrompt(text="")


def test_settings_default_to_claude_default_model_and_restricted_tools() -> None:
    assert Settings() == Settings(model=None, tool_access="restricted")


def test_update_settings_round_trip() -> None:
    command = UpdateSettings(settings=Settings(model="opus", tool_access="full"))
    assert command_adapter.validate_json(command.model_dump_json()) == command


def test_settings_changed_round_trip() -> None:
    event = SettingsChanged(settings=Settings(tool_access="off"), model_choices=("opus", "sonnet"))
    assert event_adapter.validate_json(event.model_dump_json()) == event


def test_rejects_unknown_tool_access() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"tool_access": "sometimes"})


def test_task_commands_round_trip() -> None:
    task = TaskSpec(prompt="fix the bug", settings=Settings(model="opus", tool_access="full"))
    for command in (AddTask(task=task), EditTask(task_id="1", task=task), RunTasks()):
        assert command_adapter.validate_json(command.model_dump_json()) == command
    assert TaskSpec(prompt="write docs").settings == Settings()


def test_rejects_empty_task_prompt() -> None:
    with pytest.raises(ValidationError):
        TaskSpec(prompt="")


def test_task_state_starts_pending() -> None:
    assert TaskState(id="1", task=TaskSpec(prompt="x")).status == "pending"
    assert TaskState(id="1", task=TaskSpec(prompt="x")).fixes is None
    assert StepState(name="Unit tests").status == "pending"


def test_state_and_log_events_round_trip() -> None:
    events = (
        BusyChanged(busy=True),
        TasksChanged(
            tasks=(
                TaskState(id="1", task=TaskSpec(prompt="one"), status="unverified"),
                TaskState(id="2", task=TaskSpec(prompt="fix it"), fixes="1"),
                TaskState(id="3", task=TaskSpec(prompt="three"), status="verifying"),
            )
        ),
        VerificationStarted(task_id="3", step=0),
        VerificationSkipped(task_id="2", reason="no_changes"),
        VerificationChanged(steps=(StepState(name="Lint", status="skipped"),)),
        TaskStarted(task_id="2"),
        VerificationChanged(steps=(StepState(name="Unit tests", status="failed"),)),
        VerificationFinished(task_id="1", step=0, passed=False),
    )
    for event in events:
        assert event_adapter.validate_json(event.model_dump_json()) == event


def test_rejects_unknown_task_status() -> None:
    with pytest.raises(ValidationError):
        TaskState.model_validate({"id": "1", "task": {"prompt": "x"}, "status": "sleeping"})
