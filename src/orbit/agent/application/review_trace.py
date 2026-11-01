"""Tracing reviews: the ``orbit.review`` span of each review the review lane runs, starting its
own trace and linked to the span current as orbit queued it (the remaining spans plan's
decisions 1 and 9).

The review's span is the current one while the review runs and is followed up, so the
reviewer's run records on it, and what the review's end starts, the review of its batch or a run
of its suggestions, links to it.
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

from orbit.agent.application.span_usage import usage_attributes
from orbit.agent.application.task_trace import TaskFailed
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.reviews import ReviewJob
from orbit.protocol import TaskState
from orbit.tracing import AttributeValue, NoTelemetry, Span, Telemetry, no_span, span_block

REVIEW_SPAN = "orbit.review"


def _no_session() -> str:
    return ""


@dataclass(frozen=True)
class Queued:
    """What orbit queued a review for: to review ``job``, its suggestions joining ``batch``, at
    most ``cap`` of them; ``cause`` is the span current as it was queued, unless the review was
    already queued when the session was last opened. A lens's review names its role as
    ``lens``, none otherwise."""

    job: ReviewJob
    batch: str
    cap: int
    cause: Span | None = None
    lens: str | None = None


@dataclass
class ReviewTally:
    """The suggestions a review ``made`` and those ``refused``, so far; for a lens's review,
    the ``findings`` it reported, none for another; and for a merge pass, once it has ended,
    how many of its review's findings stand at each outcome, by name, its ``outcomes``."""

    made: int = 0
    refused: int = 0
    findings: int | None = None
    outcomes: Mapping[str, int] | None = None

    def found(self) -> None:
        """Count a finding the review reported."""
        self.findings = (self.findings or 0) + 1

    def count(self, *, refused: bool) -> None:
        """Count a suggestion the review made, or one ``refused``."""
        if refused:
            self.refused += 1
        else:
            self.made += 1


@dataclass(frozen=True)
class ReviewTracing:
    """What traces a review lane's reviews: the ``telemetry`` recording them, and the ids of
    the ``workspace`` and of the ``session`` open there now."""

    telemetry: Telemetry = field(default_factory=NoTelemetry)
    workspace: str = ""
    session: Callable[[], str] = _no_session

    @contextmanager
    def review(
        self, board: TaskBoard, task_id: str, queued: Queued | None
    ) -> Iterator[ReviewTally]:
        """Review task ``task_id`` of ``board``'s span, current for the block and starting its
        own trace: of the review orbit ``queued``, linked to what queued it, or of one the user
        queued. It yields the tally of the review's suggestions; on leaving the block, the span
        is given that tally and the review's usage as the board has it, and ended."""
        attributes = self._identity(board.get(task_id), queued)
        with no_span():  # So the span starts its own trace.
            span = self.telemetry.start(REVIEW_SPAN, attributes, _links(queued))
        tally = ReviewTally(findings=None if queued is None or queued.lens is None else 0)
        with span_block(span):
            escaped = True
            try:
                yield tally
                escaped = False
            finally:
                # An error escaping is recorded as itself as the span ends.
                _ended(span, board.find(task_id), tally, failed=escaped)

    def _identity(self, state: TaskState, queued: Queued | None) -> dict[str, AttributeValue]:
        """The attributes the span of review ``state``, which orbit ``queued`` if it did,
        starts with: what queued it, the trigger by name or ``manual`` or ``planner``, as its
        origin says, and the model its own settings name, if they name one, as it's launched
        with that."""
        attributes: dict[str, AttributeValue] = {
            "orbit.workspace.id": self.workspace,
            "orbit.session.id": self.session(),
            "orbit.role": state.task.role,
            "orbit.trigger": state.origin,
        }
        if state.task.settings.model is not None:
            attributes["gen_ai.request.model"] = state.task.settings.model
        if queued is not None:
            attributes |= {
                "orbit.review.commits": len(queued.job.commits),
                "orbit.review.round": queued.job.round,
                "orbit.review.batch": queued.batch,
                "orbit.review.cap": queued.cap,
            }
        if queued is not None and queued.lens is not None:
            attributes["orbit.lens"] = queued.lens
        return attributes


def _links(queued: Queued | None) -> tuple[Span, ...]:
    """What the span of the review orbit ``queued``, if it did, links to: what queued it, if
    that's known."""
    if queued is None or queued.cause is None:
        return ()
    return (queued.cause,)


def _ended(span: Span, state: TaskState | None, tally: ReviewTally, *, failed: bool) -> None:
    """Give ``span`` the suggestions its review made and had refused, as ``tally`` counts them,
    and what the review, ``state`` on the board if it's still there, cost, recording an error
    if it ended in one, unless one escaping it already ``failed`` it."""
    span.set_attributes(
        {"orbit.review.suggestions": tally.made, "orbit.review.refused": tally.refused}
    )
    span.set_attributes(_finding_attributes(tally))
    if state is None:
        return
    span.set_attributes(usage_attributes(state.usage))
    if state.status == "error" and not failed:
        span.error(state.reason, TaskFailed(state.reason))


def _finding_attributes(tally: ReviewTally) -> dict[str, AttributeValue]:
    """What ``tally`` counts of findings: those a lens reported, and the outcomes of its
    review's findings as a merge pass ended; none for another review."""
    reported: dict[str, AttributeValue] = (
        {} if tally.findings is None else {"orbit.review.findings": tally.findings}
    )
    outcomes = tally.outcomes or {}
    return reported | {f"orbit.findings.{name}": count for name, count in outcomes.items()}
