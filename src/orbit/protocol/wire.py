"""The envelopes as JSON text, one message per frame, and back.

Decoding is tolerant: it drops fields it doesn't know and skips event types it doesn't know,
so a peer's additive changes don't break it.
"""

from typing import Any

from pydantic import ValidationError
from pydantic_core import from_json

from orbit.protocol.commands import Command
from orbit.protocol.envelope import CommandEnvelope, EventEnvelope
from orbit.protocol.events import EVENT_TYPES
from orbit.protocol.models import message_types, names_unknown_type

_WIRE = {"wire": True}
_COMMAND_TYPES = message_types(Command)


class ProtocolError(Exception):
    """Text that isn't a valid envelope, saying why in ``message``; ``command_id`` is the
    envelope's when it could be read, and ``unknown`` marks a command type this orbit doesn't
    know."""

    def __init__(self, message: str, command_id: str | None = None, unknown: bool = False):
        super().__init__(message)
        self.message = message
        self.command_id = command_id
        self.unknown = unknown


def encode(envelope: CommandEnvelope | EventEnvelope) -> str:
    """``envelope`` as compact JSON, with every field, nulls and defaults included."""
    return envelope.model_dump_json()


def decode_command(text: str) -> CommandEnvelope:
    """The command envelope ``text`` holds; raises ``ProtocolError`` if it holds none."""
    data = _load(text)
    command_id = data.get("command_id")
    if not isinstance(command_id, str) or not command_id:
        command_id = None
    if names_unknown_type(data, _COMMAND_TYPES):
        raise ProtocolError("Unknown command type", command_id, unknown=True)
    try:
        return CommandEnvelope.model_validate(data, context=_WIRE)
    except ValidationError as error:
        raise ProtocolError(str(error), command_id) from error


def decode_event(text: str) -> EventEnvelope | None:
    """The event envelope ``text`` holds, or ``None`` for an event type this orbit doesn't
    know, to be skipped; raises ``ProtocolError`` if it holds neither."""
    data = _load(text)
    if names_unknown_type(data, EVENT_TYPES):
        return None
    try:
        return EventEnvelope.model_validate(data, context=_WIRE)
    except ValidationError as error:
        raise ProtocolError(str(error)) from error


def _load(text: str) -> dict[str, Any]:
    """The JSON object ``text`` holds; raises ``ProtocolError`` if it holds none."""
    try:
        data = from_json(text)
    except ValueError as error:
        raise ProtocolError(f"Not JSON: {error}") from error
    if not isinstance(data, dict):
        raise ProtocolError("Not a JSON object")
    return data
