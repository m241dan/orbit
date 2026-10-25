"""Kiro's stream-json output, translated into orbit's events.

``kiro-cli chat --output-format stream-json`` prints a run's ACP events as JSON lines. Kiro
streams the agent's answers, and its thoughts, in chunks, which are joined into whole messages,
and charges for a run in credits. Besides its events, a run's stream tells its runner the session
to resume, whether Kiro found reasoning unavailable, Kiro's reason for stopping, and the stage a
failed run failed at.
"""

import json
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from orbit.protocol import (
    AGENT_FAILED,
    AssistantMessage,
    ErrorOccurred,
    Event,
    ToolCallFinished,
    ToolCallStarted,
    TurnEndReason,
    TurnFinished,
    TurnStarted,
    Usage,
)

# How each of Kiro's stop reasons ends a turn; any other completes it, new ones being harmless.
STOP_REASONS: dict[str, TurnEndReason] = {
    "end_turn": "completed",
    "cancelled": "cancelled",
    "max_tokens": "max_steps",
    "max_turn_requests": "max_steps",
}
# The argument Kiro adds to every tool call, saying why the model makes it.
PURPOSE_ARGUMENT = "__tool_use_purpose"
# The statuses of a tool call that has ended, and the one of a call that failed.
ENDED_STATUSES = ("completed", "failed")
FAILED_STATUS = "failed"
CREDIT = "credit"


@dataclass(frozen=True)
class _Chunk:
    """A piece of the agent's answer, or of its thoughts, as the update ``kind`` says."""

    kind: str
    text: str


class KiroTranslation(Iterator[Event]):
    """Turn ``turn_id``'s events from the lines of a Kiro run's stream, and what else the
    stream tells: the session to resume, whether reasoning was reported unavailable, Kiro's
    reason for stopping, and the stage a failed run failed at, such as ``init`` for one that
    never started. A runner reading lines as they come feeds them one at a time instead."""

    def __init__(self, turn_id: str, lines: Iterable[str] = ()) -> None:
        self.turn_id = turn_id
        self.session_id: str | None = None
        self.reasoning_unavailable = False
        self.stop_reason: str | None = None
        self.error_stage: str | None = None
        self._held: _Chunk | None = None  # The message being joined, until another event.
        self._credits = 0.0
        self._duration_ms = 0
        self._handlers: dict[str, Callable[[dict[str, Any]], Sequence[Event | _Chunk]]] = {
            "runStarted": self._started,
            "metadata": self._metadata,
            "sessionUpdate": self._updated,
            "runFinished": self._finished,
            "runError": self._failed,
        }
        self._events = self._translated(lines)

    def __next__(self) -> Event:
        return next(self._events)

    def _translated(self, lines: Iterable[str]) -> Iterator[Event]:
        for line in lines:
            yield from self.feed(line)
        yield from self.end()

    def feed(self, line: str) -> list[Event]:
        """The events ``line`` completes: none for a line that isn't a JSON object or is of an
        unknown type. A known event missing what it should hold raises RuntimeError."""
        message = _message(line)
        if message is None:
            return []
        try:
            pieces = self._pieces(message)
        except KeyError, TypeError, AttributeError:
            raise RuntimeError(f"Unexpected output from kiro-cli: {line[:200]}") from None
        return [event for piece in pieces for event in self._joined(piece)]

    def end(self) -> list[Event]:
        """The events the stream's end completes: the message being joined, if any."""
        return self._release()

    def _joined(self, piece: Event | _Chunk) -> list[Event]:
        """The events ``piece`` completes: none for a chunk continuing the message held, which
        joins it; otherwise the message held, if any, then ``piece``, held if it's a chunk."""
        if isinstance(piece, _Chunk) and self._held and piece.kind == self._held.kind:
            self._held = _Chunk(piece.kind, self._held.text + piece.text)
            return []
        released = self._release()
        if isinstance(piece, _Chunk):
            self._held = piece
            return released
        return [*released, piece]

    def _release(self) -> list[Event]:
        """The message held, unless it has no text, which is no longer held."""
        held, self._held = self._held, None
        if held is None or not held.text:
            return []
        return [AssistantMessage(turn_id=self.turn_id, text=held.text)]

    def _pieces(self, message: dict[str, Any]) -> Sequence[Event | _Chunk]:
        """The events and chunks of the stream's ``message``, noting what else it tells: none
        of an unknown type."""
        handler = self._handlers.get(message.get("type", ""))
        return [] if handler is None else handler(self._data(message))

    def _data(self, message: dict[str, Any]) -> dict[str, Any]:
        """What ``message`` holds, noting the session it names, if any."""
        data: dict[str, Any] = message.get("data") or {}
        if session_id := data.get("sessionId"):
            self.session_id = session_id
        return data

    def _started(self, _data: dict[str, Any]) -> list[Event]:
        """The start of the turn a ``runStarted`` event reports."""
        return [TurnStarted(turn_id=self.turn_id)]

    def _metadata(self, data: dict[str, Any]) -> list[Event]:
        """No events: just what a ``metadata`` event's ``data`` tells about reasoning and the
        turn's cost, noted."""
        if (data.get("reasoning") or {}).get("support") == "unavailable":
            self.reasoning_unavailable = True
        metering = data.get("meteringUsage") or []
        self._credits += sum(entry["value"] for entry in metering if entry.get("unit") == CREDIT)
        self._duration_ms = data.get("turnDurationMs", self._duration_ms)
        return []

    def _updated(self, data: dict[str, Any]) -> Sequence[Event | _Chunk]:
        """The events and chunks of a ``sessionUpdate`` event's ``data``."""
        return _update_pieces(self.turn_id, data["update"])

    def _finished(self, data: dict[str, Any]) -> list[Event]:
        """The end of the turn a ``runFinished`` event's ``data`` reports, by its stop reason."""
        self.stop_reason = data.get("stopReason")
        reason = STOP_REASONS.get(self.stop_reason or "", "completed")
        return [TurnFinished(turn_id=self.turn_id, reason=reason, usage=self._usage())]

    def _failed(self, data: dict[str, Any]) -> list[Event]:
        """The error a ``runError`` event's ``data`` reports, then the turn's end in error, noting
        the stage it failed at."""
        self.error_stage = data.get("stage")
        return [
            ErrorOccurred(turn_id=self.turn_id, code=AGENT_FAILED, message=data["message"]),
            TurnFinished(turn_id=self.turn_id, reason="error", usage=self._usage()),
        ]

    def _usage(self) -> Usage:
        """What the turn has cost so far: credits and time, as Kiro reports no tokens or dollars."""
        return Usage(credits=self._credits, duration_ms=self._duration_ms)


def translate_kiro(turn_id: str, lines: Iterable[str]) -> KiroTranslation:
    """Turn ``turn_id``'s events from the ``lines`` of a Kiro run's stream, which also tell the
    session to resume, whether reasoning was reported unavailable, and Kiro's stop reason."""
    return KiroTranslation(turn_id, lines)


def _message(line: str) -> dict[str, Any] | None:
    """The JSON object ``line`` holds, if it holds one."""
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return None
    return message if isinstance(message, dict) else None


def _update_pieces(turn_id: str, update: dict[str, Any]) -> Sequence[Event | _Chunk]:
    """The events and chunks of a session ``update``: none of a kind not known here."""
    match kind := update["sessionUpdate"]:
        case "agent_message_chunk" | "agent_thought_chunk":
            return [_Chunk(kind, update["content"].get("text", ""))]
        case "tool_call":
            return [_call_started(turn_id, update)]
        case "tool_call_update":
            return _call_ended(turn_id, update)
        case _:
            return []


def _call_started(turn_id: str, update: dict[str, Any]) -> ToolCallStarted:
    """The start of the tool call ``update`` announces, under Kiro's name for the tool (or the
    call's title when it gives none), with the arguments the model chose."""
    name = (update.get("_meta") or {}).get("kiro", {}).get("toolName") or update["title"]
    raw_input = update.get("rawInput") or {}
    arguments = {key: value for key, value in raw_input.items() if key != PURPOSE_ARGUMENT}
    return ToolCallStarted(
        turn_id=turn_id, call_id=update["toolCallId"], name=name, arguments=arguments
    )


def _call_ended(turn_id: str, update: dict[str, Any]) -> list[Event]:
    """The end of the tool call ``update`` reports, if it ended: completed or failed."""
    status = update.get("status")
    if status not in ENDED_STATUSES:
        return []
    return [
        ToolCallFinished(
            turn_id=turn_id,
            call_id=update["toolCallId"],
            output=_tool_output(update.get("rawOutput")),
            is_error=status == FAILED_STATUS,
        )
    ]


def _tool_output(raw: dict[str, Any] | None) -> str:
    """A tool's ``raw`` output as text: its ``Text`` items, a line each, or else its JSON; none
    when there is none."""
    if raw is None:
        return ""
    texts = [item["Text"] for item in raw.get("items", []) if "Text" in item]
    return "\n".join(texts) if texts else json.dumps(raw)
