"""Tracing the planner: the ``orbit.planner.turn`` span of each of its turns, a child of the
``SendMessage`` command that asked for it (the remaining spans plan's decisions 1 and 8), and
the ``orbit.consultation`` span of each turn answering a consultation, starting its own trace and
linked to the asking turn's span (the consultants plan's decision 14); a turn hearing delivered
replies starts its own trace too, linked to the span of each consultation it hears from.

The turn's span is the current one while the turn runs, so the planner records on it, through
:func:`current_span`, what the turn cost, the drafts it proposed, how it ended and the errors it
reported.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from orbit.agent.application.span_usage import usage_attributes
from orbit.protocol import TurnEndReason, Usage
from orbit.tracing import (
    AttributeValue,
    NoTelemetry,
    Span,
    Telemetry,
    no_span,
    span_block,
    use_span,
)

TURN_SPAN = "orbit.planner.turn"
CONSULTATION_SPAN = "orbit.consultation"


def _no_session() -> str:
    return ""


@dataclass(frozen=True)
class PlannerTracing:
    """What traces the planner's turns: the ``telemetry`` recording them, and the ids of the
    ``workspace`` and of the ``session`` open there now."""

    telemetry: Telemetry = field(default_factory=NoTelemetry)
    workspace: str = ""
    session: Callable[[], str] = _no_session

    @contextmanager
    def turn(
        self, parent: Span, model: str | None, links: tuple[Span, ...] = (), *, root: bool = False
    ) -> Iterator[Span]:
        """The span of a planner turn on ``model``, if known, current for the block and ended on
        leaving it: a child of ``parent``, the span current as the turn was asked for; or, when
        ``root``, starting its own trace linked to each of ``links``, the spans whose work it
        hears. An error escaping the block is recorded on it."""
        with no_span() if root else use_span(parent):
            span = self.telemetry.start(TURN_SPAN, self._identity(model), links)
        with span_block(span):
            yield span

    @contextmanager
    def consultation(
        self, cause: Span, asker: str, consultant: str, wait_ms: int
    ) -> Iterator[Span]:
        """The span of a turn of ``consultant`` answering ``asker``'s consultation, which waited
        ``wait_ms`` milliseconds in line, current for the block and ended on leaving it: it
        starts its own trace, linked to ``cause``, the asking turn's span."""
        attributes: dict[str, AttributeValue] = {
            **self._identity(None),
            "orbit.consultation.asker": asker,
            "orbit.consultation.consultant": consultant,
            "orbit.consultation.wait_ms": wait_ms,
        }
        with no_span():  # So the span starts its own trace.
            span = self.telemetry.start(CONSULTATION_SPAN, attributes, (cause,))
        with span_block(span):
            yield span

    def _identity(self, model: str | None) -> dict[str, AttributeValue]:
        """The attributes a turn's span on ``model``, if known, starts with."""
        attributes: dict[str, AttributeValue] = {
            "orbit.workspace.id": self.workspace,
            "orbit.session.id": self.session(),
        }
        if model is not None:
            attributes["gen_ai.request.model"] = model
        return attributes


def turn_ended(span: Span, reason: TurnEndReason, usage: Usage, drafts: int) -> None:
    """Record on ``span`` that its turn ended for ``reason``, having cost ``usage`` and proposed
    ``drafts`` draft tasks."""
    span.set_attributes(
        {
            **usage_attributes(usage),
            "orbit.planner.drafts": drafts,
            "orbit.planner.end_reason": reason,
        }
    )
