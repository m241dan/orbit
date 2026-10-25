"""Tracing the startup check: the ``orbit.startup_check`` span of each run, a root, with an
``orbit.verify.step`` child for each step it runs (the remaining spans plan's decision 12).

The run's span is the current one while it runs, so the check records its result on it through
:func:`current_span`, and the events it publishes carry its trace context.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from orbit.agent.application.agent_trace import traced_steps
from orbit.agent.application.ports import StepRunner
from orbit.agent.application.task_trace import VERIFY_RESULT
from orbit.tracing import AttributeValue, NoTelemetry, Span, Telemetry, current_span, no_span

STARTUP_SPAN = "orbit.startup_check"

FIX_ADDED = "orbit.startup_check.fix_added"


@dataclass(frozen=True)
class StartupTracing:
    """What traces the startup check's runs: the ``telemetry`` recording them, and the id of
    the ``workspace`` it checks."""

    telemetry: Telemetry = field(default_factory=NoTelemetry)
    workspace: str = ""

    @contextmanager
    def run(self) -> Iterator[Span]:
        """The span of a run of the check, current for the block, starting its own trace, and
        ended on leaving it, with no fix added unless :func:`fix_added` says so; a block
        cancelled is a run cancelled, and an error escaping it is recorded on it."""
        attributes: dict[str, AttributeValue] = {
            "orbit.workspace.id": self.workspace,
            FIX_ADDED: False,
        }
        with no_span(), self.telemetry.span(STARTUP_SPAN, attributes) as span:
            try:
                yield span
            except asyncio.CancelledError:
                span.set_attributes({VERIFY_RESULT: "cancelled"})
                raise

    def steps(self, run_step: StepRunner) -> StepRunner:
        """``run_step``, timing each step it runs as a child of the current span, the run's."""
        return traced_steps(self.telemetry, current_span(), run_step)


def fix_added() -> None:
    """Record on the current span, a run's, that it added a draft task to fix the step that
    failed."""
    current_span().set_attributes({FIX_ADDED: True})
