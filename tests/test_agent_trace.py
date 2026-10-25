"""Each agent's ``orbit.agent.process`` span, under what it works for, with each of its calls to
orbit an ``orbit.agent.request`` child and each verification step a call runs an
``orbit.verify.step`` child of that call's span."""

import asyncio
import contextvars
from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode

from orbit.agent import ControlPlane, Verification, VerificationStep, WorkspaceHost
from orbit.agent.application.agent_trace import (
    PROCESS_SPAN,
    REQUEST_SPAN,
    STEP_SPAN,
    tool_attributes,
)
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.planner_trace import TURN_SPAN
from orbit.agent.application.ports import CallHandler, Reply
from orbit.agent.application.review_trace import REVIEW_SPAN
from orbit.agent.application.task_trace import RUN_SPAN
from orbit.protocol import (
    CODER,
    PLANNER,
    REVIEWER,
    AddTask,
    CancelTurn,
    Event,
    RunTasks,
    SendMessage,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import (
    STEP,
    Call,
    FakeHost,
    FakeTree,
    Script,
    ScriptedLauncher,
    Steps,
    agents_for,
    claims_done,
    close,
    events_until,
    idle,
    proposes_two,
    run,
    send,
    session_for,
    settle,
    suggests,
)
from tests.telemetry_helpers import Traced

LINT = VerificationStep(name="Lint", command=("ruff", "check"))


class ServerHost(FakeHost):
    """A host handling each call in the context it started in, as an MCP server handles its
    requests, rather than in the agent's."""

    def __init__(self) -> None:
        super().__init__()
        self._context = contextvars.Context()

    async def start(self, handler: CallHandler) -> str:
        self._context = contextvars.copy_context()
        return await super().start(handler)

    async def call(self, token: str, tool: str, **arguments: Any) -> Reply:
        return await asyncio.create_task(
            super().call(token, tool, **arguments), context=self._context
        )


def attributes(span: ReadableSpan) -> Mapping[str, Any]:
    assert span.attributes is not None
    return span.attributes


def child_of(child: ReadableSpan, parent: ReadableSpan) -> bool:
    assert child.parent is not None and parent.context is not None
    return bool(child.parent.span_id == parent.context.span_id)


def in_order(spans: Sequence[ReadableSpan]) -> list[ReadableSpan]:
    """``spans``, in the order they started."""
    return sorted(spans, key=lambda span: span.start_time or 0)


def requests(traced: Traced) -> list[ReadableSpan]:
    """The calls' spans, in the order the calls were made."""
    return in_order(traced.spans(REQUEST_SPAN))


def tools(spans: Sequence[ReadableSpan]) -> list[str]:
    """The tools called in ``spans``, calls' spans, in order."""
    return [attributes(span)["orbit.agent.tool"] for span in spans]


def steps_under(traced: Traced, request: ReadableSpan) -> list[Mapping[str, Any]]:
    """The attributes of the step spans that are children of ``request``, in order."""
    steps = in_order(traced.spans(STEP_SPAN))
    return [dict(attributes(step)) for step in steps if child_of(step, request)]


def traced_session(traced: Traced, *scripts: Script, **options: Any) -> WorkspaceHost:
    """A session whose agents play ``scripts``, traced by ``traced``, reaching orbit through a
    host that handles their calls as an MCP server does; ``options`` go to
    :func:`session_for`."""
    session, _, _ = session_for(*scripts, telemetry=traced.telemetry, host=ServerHost(), **options)
    return session


async def checks_twice(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("run_check", step=STEP.name)
    await call("run_check", step="Nope")
    await call("complete_task", summary="done")
    return "completed"


async def claims_twice(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("complete_task", summary="done")
    await call("complete_task", summary="fixed")
    return "completed"


async def ends_in_error(call: Call) -> TurnEndReason:
    await call("get_task")
    return "error"


async def fails(call: Call) -> TurnEndReason:
    raise RuntimeError("Could not start 'claude'.")


def test_tool_attributes_name_the_tools_sorted_with_their_counts_aligned() -> None:
    assert tool_attributes({"Read": 2, "Bash": 1, "Edit": 4}) == {
        "orbit.agent.tool_names": ["Bash", "Edit", "Read"],
        "orbit.agent.tool_counts": [1, 4, 2],
        "orbit.agent.tool_calls_total": 7,
    }


def test_tool_attributes_of_no_calls_are_empty() -> None:
    assert tool_attributes({}) == {
        "orbit.agent.tool_names": [],
        "orbit.agent.tool_counts": [],
        "orbit.agent.tool_calls_total": 0,
    }


async def test_a_tasks_agent_is_a_process_under_its_run_with_its_tools_tallied() -> None:
    traced = Traced()
    session, _, launcher = session_for(claims_done, telemetry=traced.telemetry, host=ServerHost())
    launcher.tool_calls = [("Edit", "Bash", "Bash", "Bash")]
    await asyncio.wait_for(run(session), 5)

    [run_span] = traced.spans(RUN_SPAN)
    [process] = traced.spans(PROCESS_SPAN)
    assert child_of(process, run_span)
    assert dict(attributes(process)) == {
        "orbit.agent.id": "agent-1",
        "orbit.role": CODER,
        "orbit.runtime": "claude",
        "orbit.agent.exit_reason": "finished",
        "orbit.agent.turns": 1,
        "orbit.agent.tool_names": ("Bash", "Edit"),
        "orbit.agent.tool_counts": (3, 1),
        "orbit.agent.tool_calls_total": 4,
    }
    assert process.status.status_code is StatusCode.UNSET


async def test_an_agents_calls_are_requests_under_its_process() -> None:
    traced = Traced()
    session = traced_session(traced, claims_done)
    await asyncio.wait_for(run(session), 5)

    [process] = traced.spans(PROCESS_SPAN)
    get_task, complete_task = requests(traced)
    assert child_of(get_task, process) and child_of(complete_task, process)
    assert dict(attributes(get_task)) == {
        "orbit.agent.tool": "get_task",
        "orbit.agent.request.error": False,
    }
    assert tools([complete_task]) == ["complete_task"]


async def test_each_step_verifying_a_claim_is_a_span_under_its_request() -> None:
    traced = Traced()
    session = traced_session(
        traced, claims_done, verification=Verification((STEP, LINT), Steps(True, True))
    )
    await asyncio.wait_for(run(session), 5)

    _, complete_task = requests(traced)
    assert steps_under(traced, complete_task) == [
        {"orbit.verify.step.name": "Unit tests", "orbit.verify.step.result": "passed"},
        {"orbit.verify.step.name": "Lint", "orbit.verify.step.result": "passed"},
    ]
    task_steps = [e for e in traced.task().events if e.name == "orbit.verify.step"]
    assert [(e.attributes or {})["name"] for e in task_steps] == ["Unit tests", "Lint"]


async def test_a_step_that_fails_a_claim_is_a_span_with_its_result() -> None:
    traced = Traced()
    session = traced_session(
        traced, claims_twice, verification=Verification((STEP,), Steps(False, True))
    )
    await asyncio.wait_for(run(session), 5)

    _, first, second = requests(traced)
    [failed] = steps_under(traced, first)
    [passed] = steps_under(traced, second)
    assert failed["orbit.verify.step.result"] == "failed"
    assert passed["orbit.verify.step.result"] == "passed"


async def test_a_step_an_agent_runs_itself_is_a_span_under_its_request() -> None:
    traced = Traced()
    session = traced_session(
        traced, checks_twice, verification=Verification((STEP,), Steps(True, True))
    )
    await asyncio.wait_for(run(session), 5)

    calls = requests(traced)
    assert tools(calls) == ["get_task", "run_check", "run_check", "complete_task"]
    _, check, unknown, complete_task = calls
    assert steps_under(traced, check) == [
        {"orbit.verify.step.name": "Unit tests", "orbit.verify.step.result": "passed"}
    ]
    assert attributes(check)["orbit.agent.request.error"] is False
    assert steps_under(traced, unknown) == []
    assert attributes(unknown)["orbit.agent.request.error"] is True
    assert len(steps_under(traced, complete_task)) == 1


async def test_an_agent_whose_turn_ends_in_error_crashed() -> None:
    traced = Traced()
    session = traced_session(traced, ends_in_error)
    await asyncio.wait_for(run(session), 5)

    [process] = traced.spans(PROCESS_SPAN)
    assert attributes(process)["orbit.agent.exit_reason"] == "crashed"
    assert process.status.status_code is StatusCode.UNSET


async def test_an_agent_whose_process_fails_crashed_with_the_error_status() -> None:
    traced = Traced()
    session = traced_session(traced, fails)
    await asyncio.wait_for(run(session), 5)

    [process] = traced.spans(PROCESS_SPAN)
    assert attributes(process)["orbit.agent.exit_reason"] == "crashed"
    assert attributes(process)["orbit.agent.turns"] == 1
    assert process.status.status_code is StatusCode.ERROR
    assert process.status.description == "Could not start 'claude'."
    assert [event.name for event in process.events] == ["exception"]


async def test_an_agent_the_user_stops_was_killed() -> None:
    traced = Traced()
    working = asyncio.Event()

    async def works_forever(call: Call) -> TurnEndReason:
        await call("get_task")
        working.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session = traced_session(traced, works_forever)
    await send(session, AddTask(task=TaskSpec(prompt="a")))
    await send(session, RunTasks())
    await asyncio.wait_for(working.wait(), 5)
    await send(session, CancelTurn())
    await events_until(session, idle())

    [process] = traced.spans(PROCESS_SPAN)
    assert attributes(process)["orbit.agent.exit_reason"] == "killed"
    assert process.status.status_code is StatusCode.UNSET


def traced_plane(
    traced: Traced, *scripts: Script, **options: Any
) -> tuple[ControlPlane, ScriptedLauncher]:
    """A control plane serving a host whose agents play ``scripts``, both traced by
    ``traced``, and the launcher playing them; ``options`` go to :func:`session_for`."""
    host, _, launcher = session_for(
        *scripts, telemetry=traced.telemetry, host=ServerHost(), **options
    )
    return ControlPlane([host], telemetry=traced.telemetry), launcher


async def test_a_planner_turns_process_is_under_the_turn_with_its_calls_under_it() -> None:
    traced = Traced()
    plane, launcher = traced_plane(traced)
    launcher.planner_turns = [proposes_two]
    await send(plane, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(plane, idle("planner"))
    await close(plane)

    [turn] = traced.spans(TURN_SPAN)
    [process] = traced.spans(PROCESS_SPAN)
    assert child_of(process, turn)
    assert attributes(process)["orbit.role"] == PLANNER
    assert attributes(process)["orbit.agent.exit_reason"] == "finished"
    assert attributes(process)["orbit.agent.turns"] == 1
    calls = requests(traced)
    assert tools(calls) == ["propose_task", "propose_task"]
    assert all(child_of(call, process) for call in calls)


async def test_a_planner_process_ending_its_turn_in_error_crashed() -> None:
    traced = Traced()
    plane, launcher = traced_plane(traced)
    launcher.planner_turns = [proposes_two]
    launcher.planner_errors = ["API Error: 529 Overloaded"]
    await send(plane, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(plane, idle("planner"))
    await close(plane)

    [process] = traced.spans(PROCESS_SPAN)
    assert attributes(process)["orbit.agent.exit_reason"] == "crashed"


async def test_a_reviewers_process_is_under_its_review() -> None:
    traced = Traced()
    plane, _ = traced_plane(
        traced, claims_done, reviewers=(suggests(),), tree=FakeTree(), review=True
    )
    await send(plane, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(plane, RunTasks())
    await asyncio.wait_for(settle(plane), 10)
    await close(plane)

    [review] = traced.spans(REVIEW_SPAN)
    [process] = [
        span for span in traced.spans(PROCESS_SPAN) if attributes(span)["orbit.role"] == REVIEWER
    ]
    assert child_of(process, review)


async def test_a_call_from_an_agent_whose_process_isnt_traced_is_under_the_span_current() -> None:
    traced = Traced()

    async def publish(event: Event) -> None:
        pass

    agents, _ = agents_for()
    desk = AgentDesk(agents, publish, traced.telemetry)

    class Target:
        async def handle(self, tool: str, arguments: dict[str, Any]) -> Reply:
            return Reply("Noted.")

    agent = desk.open(Target(), CODER, "1")
    with traced.telemetry.span("outer"):
        await desk.handle(agent.token, "report_progress", {})

    [outer] = traced.spans("outer")
    [request] = traced.spans(REQUEST_SPAN)
    assert child_of(request, outer)
