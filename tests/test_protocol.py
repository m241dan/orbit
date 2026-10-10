import pytest
from pydantic import ValidationError

from orbit.protocol import (
    Settings,
    SettingsChanged,
    SubmitPrompt,
    ToolCallStarted,
    UpdateSettings,
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
