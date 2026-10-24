"""Tracing a task: the ``orbit.task`` span, from when the task is ready to run until its status
is final, with its wait for a coder's slot and its run as children (the telemetry plan's
decisions 15 to 17).

The task's span is the current one while it runs, so what runs it, from the agent's run to its
verification and landing, records on it through :func:`current_span`.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from orbit.agent.application.coder_slots import CoderSeat
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.commits import task_title
from orbit.protocol import TaskState
from orbit.tracing import (
    AttributeValue,
    NoTelemetry,
    Span,
    Telemetry,
    current_span,
    no_span,
    span_block,
)

TASK_SPAN = "orbit.task"
WAIT_SPAN = "orbit.task.wait"
RUN_SPAN = "orbit.task.run"

RUNTIME = "claude"
"""What runs orbit's agents."""

FIX_ATTEMPTS = "orbit.task.fix_attempts"
CHECK_RUNS = "orbit.task.check_runs"
VERIFY_RESULT = "orbit.verify.result"
VERIFY_FAILED_STEP = "orbit.verify.failed_step"
MERGE_RESULT = "orbit.merge.result"

VERIFY_STEP_EVENT = "orbit.verify.step"
GAVE_UP_EVENT = "orbit.task.gave_up"
MERGE_CONFLICT_EVENT = "orbit.merge.conflict"


class TaskFailed(Exception):
    """A task that ended in error, recorded on its span as the exception it ended for."""


def _no_session() -> str:
    return ""


@dataclass(frozen=True)
class TaskTracing:
    """What traces a work lane's tasks: the ``telemetry`` recording them, and the ids of the
    ``workspace`` and of the ``session`` open there now."""

    telemetry: Telemetry = field(default_factory=NoTelemetry)
    workspace: str = ""
    session: Callable[[], str] = _no_session

    @contextmanager
    def task(self, board: TaskBoard, task_id: str, model: str | None) -> Iterator[Span]:
        """Task ``task_id`` of ``board``'s span, on ``model`` if known, current for the block
        and starting its own trace; on leaving it, it's given the task's usage, status and
        reason as the board has them, and ended."""
        attributes = self._identity(board.get(task_id), model)
        with no_span():  # So the span starts its own trace.
            span = self.telemetry.start(TASK_SPAN, attributes)
        with span_block(span):
            escaped = True
            try:
                yield span
                escaped = False
            finally:
                # An error escaping is recorded as itself as the span ends.
                _ended(span, board.find(task_id), failed=escaped)

    @contextmanager
    def run(self) -> Iterator[None]:
        """Time the block as the current task's ``orbit.task.run``, leaving the task's span
        current."""
        span = self.telemetry.start(RUN_SPAN)
        try:
            yield
        finally:
            span.end()

    async def seat(self, coders: CoderSeat) -> bool:
        """Take a coder's slot at ``coders`` for the current task, as :meth:`CoderSeat.acquire`
        does, timing a wait in line as its ``orbit.task.wait``."""
        if coders.open:
            return await coders.acquire()
        task = current_span()
        attributes = {
            "orbit.wait.position": coders.waiting + 1,
            "orbit.wait.slots_busy": coders.busy,
        }
        started = time.monotonic()
        try:
            with self.telemetry.span(WAIT_SPAN, attributes):
                return await coders.acquire()
        finally:
            task.set_attributes({"orbit.task.wait_ms": (time.monotonic() - started) * 1000})

    def _identity(self, state: TaskState, model: str | None) -> dict[str, AttributeValue]:
        """The attributes ``state``'s span starts with, on ``model`` if known."""
        attributes: dict[str, AttributeValue] = {
            "orbit.workspace.id": self.workspace,
            "orbit.session.id": self.session(),
            "orbit.task.id": state.id,
            "orbit.task.title": task_title(state.task),
            "orbit.role": state.task.role,
            "orbit.runtime": RUNTIME,
            "orbit.trigger": state.origin,
            FIX_ATTEMPTS: 0,
            "orbit.task.wait_ms": 0.0,
            CHECK_RUNS: 0,
        }
        if model is not None:
            attributes["gen_ai.request.model"] = model
        if state.fixes is not None:
            attributes["orbit.task.fixes"] = state.fixes
        return attributes


def _ended(span: Span, state: TaskState | None, *, failed: bool) -> None:
    """Give ``span`` what its task, ``state`` on the board if it's still there, cost and how it
    ended, recording an error unless one escaping it already ``failed`` it."""
    if state is None:
        return
    usage = state.usage
    span.set_attributes(
        {
            "gen_ai.usage.input_tokens": usage.input_tokens,
            "gen_ai.usage.output_tokens": usage.output_tokens,
            "gen_ai.usage.cache_creation.input_tokens": usage.cache_creation_input_tokens,
            "gen_ai.usage.cache_read.input_tokens": usage.cache_read_input_tokens,
            "orbit.cost.usd": usage.cost_usd,
            "orbit.task.status": state.status,
        }
    )
    if state.reason:
        span.set_attributes({"orbit.task.reason": state.reason})
    if state.status == "error" and not failed:
        span.error(state.reason, TaskFailed(state.reason))
