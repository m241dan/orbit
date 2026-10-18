"""The envelopes as JSON text, one message per frame, and back.

Decoding is tolerant: it drops fields it doesn't know and skips event types it doesn't know,
so a peer's additive changes don't break it.
"""

from typing import Any, get_args

from pydantic import ValidationError
from pydantic_core import from_json

from orbit.protocol.commands import Command
from orbit.protocol.envelope import CommandEnvelope, EventEnvelope
from orbit.protocol.events import Event

_WIRE = {"wire": True}


def _types(union: Any) -> frozenset[str]:
    """The ``type`` names of the messages in the discriminated ``union``."""
    return frozenset(member.model_fields["type"].default for member in get_args(get_args(union)[0]))


_COMMAND_TYPES = _types(Command)
_EVENT_TYPES = _types(Event)


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
    if _is_unknown(data, _COMMAND_TYPES):
        raise ProtocolError("Unknown command type", command_id, unknown=True)
    try:
        return CommandEnvelope.model_validate(data, context=_WIRE)
    except ValidationError as error:
        raise ProtocolError(str(error), command_id) from error


def decode_event(text: str) -> EventEnvelope | None:
    """The event envelope ``text`` holds, or ``None`` for an event type this orbit doesn't
    know, to be skipped; raises ``ProtocolError`` if it holds neither."""
    data = _load(text)
    if _is_unknown(data, _EVENT_TYPES):
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


def _is_unknown(data: dict[str, Any], known: frozenset[str]) -> bool:
    """Whether ``data``'s payload names a ``type`` that isn't one of the ``known`` ones."""
    payload = data.get("payload")
    kind = payload.get("type") if isinstance(payload, dict) else None
    return isinstance(kind, str) and kind not in known
