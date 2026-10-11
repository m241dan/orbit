import pytest
from pydantic import ValidationError

from orbit.protocol import (
    RunTasks,
    Settings,
    SettingsChanged,
    SubmitPrompt,
    TaskFinished,
    TaskInserted,
    TaskSpec,
    TaskStarted,
    ToolCallStarted,
    UpdateSettings,
    VerificationConfigured,
    VerificationFinished,
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


def test_run_tasks_round_trip() -> None:
    command = RunTasks(
        tasks=(
            TaskSpec(prompt="fix the bug", settings=Settings(model="opus", tool_access="full")),
            TaskSpec(prompt="write docs"),
        )
    )
    assert command_adapter.validate_json(command.model_dump_json()) == command
    assert command.tasks[1].settings == Settings()


def test_run_tasks_rejects_empty_list_and_empty_prompt() -> None:
    with pytest.raises(ValidationError):
        RunTasks(tasks=())
    with pytest.raises(ValidationError):
        TaskSpec(prompt="")


def test_task_events_round_trip() -> None:
    for event in (TaskStarted(index=0), TaskFinished(index=2, reason="error")):
        assert event_adapter.validate_json(event.model_dump_json()) == event


def test_verification_events_round_trip() -> None:
    events = (
        VerificationConfigured(steps=("Unit tests",)),
        VerificationStarted(index=0, step=0),
        VerificationFinished(index=0, step=0, passed=False),
        TaskInserted(index=1, task=TaskSpec(prompt="fix it")),
        TaskFinished(index=0, reason="completed", verified=False),
    )
    for event in events:
        assert event_adapter.validate_json(event.model_dump_json()) == event


def test_task_finished_is_unverified_by_default() -> None:
    assert TaskFinished(index=0, reason="error").verified is None
