"""The JSON wire encoding: every message round-trips, decoding tolerates what it doesn't know."""

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from orbit.protocol import (
    Command,
    CommandEnvelope,
    Event,
    EventEnvelope,
    Notice,
    ProtocolError,
    TasksChanged,
    TaskSpec,
    TaskState,
    decode_command,
    decode_event,
    encode,
)
from tests.protocol_examples import EXAMPLES


def members(union: Any) -> set[type]:
    """The message types of the discriminated ``union``."""
    return set(get_args(get_args(union)[0]))


def test_every_command_and_event_has_an_example() -> None:
    covered = {type(example.payload) for example in EXAMPLES}

    assert members(Command) | members(Event) <= covered


def test_commands_are_examples_in_command_envelopes() -> None:
    for example in EXAMPLES:
        expected = CommandEnvelope if type(example.payload) in members(Command) else EventEnvelope
        assert type(example) is expected


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example.payload.type)
def test_every_example_decodes_to_what_was_encoded(
    example: CommandEnvelope | EventEnvelope,
) -> None:
    decode = decode_command if isinstance(example, CommandEnvelope) else decode_event

    assert decode(encode(example)) == example


def test_encode_writes_nulls_compactly() -> None:
    text = encode(EventEnvelope(seq=1, payload=Notice(message="hi")))

    assert '"traceparent":null' in text
    assert ", " not in text
    assert ": " not in text


def test_encode_writes_defaults() -> None:
    text = encode(EventEnvelope(seq=1, payload=Notice(message="hi")))

    assert '"workspace":null' in text and '"session":null' in text


TASK_LIST = (
    '{"seq":1,"extra":true,"payload":{"type":"tasks_changed","new":1,'
    '"tasks":[{"id":"t1","task":{"prompt":"Go","later":2},"later":3}]}}'
)


def test_decode_event_drops_fields_it_doesnt_know() -> None:
    expected = EventEnvelope(
        seq=1, payload=TasksChanged(tasks=(TaskState(id="t1", task=TaskSpec(prompt="Go")),))
    )

    assert decode_event(TASK_LIST) == expected


def test_messages_built_in_process_still_refuse_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        TaskState.model_validate({"id": "t1", "task": {"prompt": "Go"}, "later": 3})


def test_envelopes_validated_without_the_wire_context_still_refuse_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        EventEnvelope.model_validate_json(TASK_LIST)


def test_decode_event_skips_an_unknown_event_type() -> None:
    assert decode_event('{"seq":1,"payload":{"type":"something_new","x":1}}') is None


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"seq":1,"payload":{"type":"notice"}}',
        '{"seq":1,"payload":{"message":"untyped"}}',
        '{"seq":1,"payload":"notice"}',
    ],
)
def test_decode_event_refuses_what_isnt_a_valid_event(text: str) -> None:
    with pytest.raises(ProtocolError) as caught:
        decode_event(text)

    assert caught.value.command_id is None
    assert not caught.value.unknown


def test_a_protocol_error_says_what_went_wrong() -> None:
    with pytest.raises(ProtocolError) as caught:
        decode_event("[]")

    assert caught.value.message == str(caught.value)
    assert "object" in caught.value.message


def test_decode_command_reads_a_command() -> None:
    text = '{"command_id":"c1","payload":{"type":"run_tasks"},"extra":1}'

    assert decode_command(text).command_id == "c1"


def test_an_invalid_known_command_carries_its_command_id() -> None:
    with pytest.raises(ProtocolError) as caught:
        decode_command('{"command_id":"c1","payload":{"type":"send_message","agent":"x"}}')

    assert caught.value.command_id == "c1"
    assert not caught.value.unknown


def test_an_unknown_command_type_is_marked_unknown() -> None:
    with pytest.raises(ProtocolError) as caught:
        decode_command('{"command_id":"c1","payload":{"type":"something_new"}}')

    assert caught.value.command_id == "c1"
    assert caught.value.unknown


@pytest.mark.parametrize(
    "text",
    [
        '{"payload":{"type":"run_tasks"}}',
        '{"command_id":"","payload":{"type":"run_tasks"}}',
        '{"command_id":5,"payload":{"type":"run_tasks"}}',
        "not json",
    ],
)
def test_a_command_without_a_readable_command_id_has_none(text: str) -> None:
    with pytest.raises(ProtocolError) as caught:
        decode_command(text)

    assert caught.value.command_id is None
