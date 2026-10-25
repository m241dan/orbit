"""Tracing a command: the ``orbit.command`` span it's handled in, from when it's received until
its outcome is published (the remaining spans plan's decision 7).

The span is the current one while the command is handled, so the work it does synchronously
nests under it and the events it publishes, its outcome among them, carry its trace context.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from orbit.agent.domain.refusals import Refused
from orbit.protocol import CommandEnvelope
from orbit.tracing import AttributeValue, Span, Telemetry, use_span

COMMAND_SPAN = "orbit.command"

OUTCOME = "orbit.command.outcome"
CODE = "orbit.command.code"


@dataclass(frozen=True)
class CommandScope:
    """Who sent a command and what it's for: the name of the ``client`` that sent it, and the
    ids of the ``workspace`` and ``session`` it's for, each if known."""

    client: str | None = None
    workspace: str | None = None
    session: str | None = None


@contextmanager
def command_span(
    telemetry: Telemetry, envelope: CommandEnvelope, scope: CommandScope
) -> Iterator[Span]:
    """The span of the command in ``envelope``, sent as ``scope`` says, current for the block:
    a child of the remote span ``envelope`` carries, if it carries one, else starting its own
    trace. An error escaping the block is recorded on it."""
    attributes = _identity(envelope, scope)
    with use_span(telemetry.carried(envelope)), telemetry.span(COMMAND_SPAN, attributes) as span:
        yield span


def accepted(span: Span) -> None:
    """Record on ``span`` that its command was accepted."""
    span.set_attributes({OUTCOME: "accepted"})


def refused(span: Span, code: str, message: str, failure: Exception) -> None:
    """Record on ``span`` that its command was refused with ``code`` for ``failure``: as an error
    with ``message`` unless it's a refusal, which is the command's doing, not orbit's."""
    span.set_attributes({OUTCOME: "refused", CODE: code})
    if not isinstance(failure, Refused):
        span.error(message, failure)


def _identity(envelope: CommandEnvelope, scope: CommandScope) -> dict[str, AttributeValue]:
    """The attributes the span of the command in ``envelope``, sent as ``scope`` says, starts
    with: those of ``scope`` that are known."""
    known = {
        "orbit.workspace.id": scope.workspace,
        "orbit.session.id": scope.session,
        "orbit.client": scope.client,
    }
    attributes: dict[str, AttributeValue] = {"orbit.command.type": envelope.payload.type}
    attributes.update({name: value for name, value in known.items() if value is not None})
    return attributes
