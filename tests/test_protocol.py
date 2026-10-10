import pytest
from pydantic import ValidationError

from orbit.protocol import (
    SubmitPrompt,
    ToolCallStarted,
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
