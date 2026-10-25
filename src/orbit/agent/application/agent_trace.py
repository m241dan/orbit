"""Tracing agents: the ``orbit.agent.process`` span of each agent's process, a child of what it
works for, with an ``orbit.agent.request`` child for each of its calls to orbit, and an
``orbit.verify.step`` child of a call for each verification step it runs (the remaining spans
plan's decisions 10 and 11).

Calls arrive in the MCP server's context, not the agent's, so the desk finds a call's process
span by the agent's token and makes it current for the call.
"""

import asyncio
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

from orbit.agent.application.ports import StepRunner
from orbit.agent.application.task_trace import RUNTIME
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import AgentExitReason, Event, ToolCallStarted, TurnFinished, TurnStarted
from orbit.tracing import AttributeValue, NoTelemetry, Span, SpanHandle, Telemetry, use_span

PROCESS_SPAN = "orbit.agent.process"
REQUEST_SPAN = "orbit.agent.request"
STEP_SPAN = "orbit.verify.step"

REQUEST_ERROR = "orbit.agent.request.error"


def tool_attributes(counts: Mapping[str, int]) -> dict[str, AttributeValue]:
    """The attributes recording the tool calls ``counts`` tallies by tool: the tools' names,
    sorted, their counts in the same order, and the calls in all."""
    names = sorted(counts)
    return {
        "orbit.agent.tool_names": names,
        "orbit.agent.tool_counts": [counts[name] for name in names],
        "orbit.agent.tool_calls_total": sum(counts.values()),
    }


class AgentProcess:
    """An agent's process, traced in ``span``, tallying the turns it starts and the tools it
    calls; its ``reason`` is how it exited, ``finished`` unless something says otherwise."""

    def __init__(self, span: SpanHandle) -> None:
        self.span = span
        self.reason: AgentExitReason = "finished"
        self._turns = 0
        self._tools: Counter[str] = Counter()

    def saw(self, event: Event) -> None:
        """Tally ``event``, which the process reported: a turn started, a tool called, or a turn
        ended in error, after which the process counts as crashed."""
        if isinstance(event, TurnStarted):
            self._turns += 1
        elif isinstance(event, ToolCallStarted):
            self._tools[event.name] += 1
        elif isinstance(event, TurnFinished) and event.reason == "error":
            self.reason = "crashed"

    def end(self) -> None:
        """End the span, recording how the process exited, its turns and its tool calls."""
        self.span.set_attributes(
            {
                "orbit.agent.exit_reason": self.reason,
                "orbit.agent.turns": self._turns,
                **tool_attributes(self._tools),
            }
        )
        self.span.end()


@dataclass(frozen=True)
class AgentTracing:
    """What traces agents' processes and their calls: the ``telemetry`` recording them."""

    telemetry: Telemetry = field(default_factory=NoTelemetry)

    @contextmanager
    def process(self, agent: Agent, parent: Span) -> Iterator[AgentProcess]:
        """``agent``'s process, traced over the block in a span that's a child of ``parent``
        and ended on leaving it: killed if the block is cancelled, and crashed if an error
        escapes it, which is recorded on it."""
        attributes = {
            "orbit.agent.id": agent.id,
            "orbit.role": agent.role,
            "orbit.runtime": RUNTIME,
        }
        with use_span(parent):
            process = AgentProcess(self.telemetry.start(PROCESS_SPAN, attributes))
        try:
            yield process
        except asyncio.CancelledError:
            process.reason = "killed"
            raise
        except Exception as error:
            process.reason = "crashed"
            process.span.error(str(error), error)
            raise
        finally:
            process.end()

    @contextmanager
    def request(self, process: Span, tool: str) -> Iterator[Span]:
        """The span of an agent's call to ``tool``, a child of ``process``, its process's span,
        current for the block and ended on leaving it."""
        attributes = {"orbit.agent.tool": tool}
        with use_span(process), self.telemetry.span(REQUEST_SPAN, attributes) as span:
            yield span


def traced_steps(telemetry: Telemetry, request: Span, run_step: StepRunner) -> StepRunner:
    """``run_step``, timing each step it runs as a span of ``telemetry``'s with how the step came
    out, a child of ``request``, the span of the agent's call it runs for."""

    async def run(step: VerificationStep, root: str) -> tuple[bool, str]:
        attributes = {"orbit.verify.step.name": step.name}
        with use_span(request), telemetry.span(STEP_SPAN, attributes) as span:
            passed, output = await run_step(step, root)
            span.set_attributes({"orbit.verify.step.result": "passed" if passed else "failed"})
            return passed, output

    return run
