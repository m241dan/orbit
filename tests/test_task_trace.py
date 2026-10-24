"""Each task's ``orbit.task`` span: its identity, what its run cost and how it ended, with its
wait for a coder and its run as children, exported through the OpenTelemetry SDK."""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from orbit.agent import Verification, VerificationStep, WorkspaceHost
from orbit.agent.application.ports import Merge
from orbit.agent.application.task_trace import TaskTracing
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.roles import role_task
from orbit.protocol import (
    CODER,
    AddTask,
    CancelTurn,
    RunTasks,
    Settings,
    TaskSpec,
    TurnEndReason,
    Usage,
)
from orbit.telemetry.otel import OtelTelemetry, otel_telemetry
from tests.agent_fakes import (
    ROLES,
    STEP,
    Call,
    FakeTree,
    Script,
    Steps,
    chats,
    claims_done,
    events_until,
    greeted,
    idle,
    keeps_claiming,
    run,
    send,
    session_for,
    settle,
    start,
    verified,
)

LINT = VerificationStep(name="Lint", command=("ruff", "check"))


class Traced:
    """Telemetry exporting its spans to memory."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.telemetry: OtelTelemetry = otel_telemetry(
            self.exporter, Resource.create({"service.name": "orbit-tests"})
        )

        self._closed = False

    def spans(self, name: str) -> list[ReadableSpan]:
        """The spans called ``name`` exported, once every span is."""
        if not self._closed:
            self._closed = True
            self.telemetry.close()
        return [span for span in self.exporter.get_finished_spans() if span.name == name]

    def task(self) -> ReadableSpan:
        """The one task's span."""
        [task] = self.spans("orbit.task")
        return task


def attributes(span: ReadableSpan) -> Mapping[str, Any]:
    assert span.attributes is not None
    return span.attributes


def child_of(child: ReadableSpan, parent: ReadableSpan) -> bool:
    assert child.parent is not None and parent.context is not None
    return bool(child.parent.span_id == parent.context.span_id)


def step_events(span: ReadableSpan) -> list[Mapping[str, Any]]:
    """The attributes of ``span``'s verification step events, in order."""
    return [dict(e.attributes or {}) for e in span.events if e.name == "orbit.verify.step"]


def traced_session(traced: Traced, *scripts: Script, **options: Any) -> WorkspaceHost:
    session, _, _ = session_for(*scripts, telemetry=traced.telemetry, **options)
    return session


async def gives_up(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("give_up", reason="The task contradicts the code.")
    return "completed"


async def claims_twice(call: Call) -> TurnEndReason:
    """Claims the task done, then again once told it failed."""
    await call("get_task")
    await call("complete_task", summary="done")
    await call("complete_task", summary="fixed")
    return "completed"


async def runs_checks(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("run_check", step=STEP.name)
    await call("run_check", step=STEP.name)
    await call("complete_task", summary="done")
    return "completed"


async def test_a_task_that_merges_is_one_root_span_with_its_run_as_child() -> None:
    traced = Traced()
    usages = (
        Usage(input_tokens=10, output_tokens=2, cost_usd=0.5),
        Usage(cache_creation_input_tokens=3, cache_read_input_tokens=4, cost_usd=0.25),
    )
    session, _, launcher = session_for(
        claims_done,
        telemetry=traced.telemetry,
        verification=Verification((STEP, LINT), Steps(True, True)),
        tree=FakeTree(),
    )
    launcher.usages = [usages]
    await asyncio.wait_for(run(session), 5)

    task = traced.task()
    [run_span] = traced.spans("orbit.task.run")
    assert task.parent is None
    assert child_of(run_span, task)
    assert traced.spans("orbit.task.wait") == []
    steps = step_events(task)
    assert [step["name"] for step in steps] == ["Unit tests", "Lint"]
    assert {step["result"] for step in steps} == {"passed"}
    assert all(step["duration_ms"] >= 0 for step in steps)
    assert dict(attributes(task)) == {
        "orbit.workspace.id": "unregistered",
        "orbit.session.id": "session-1",
        "orbit.task.id": "1",
        "orbit.task.title": "Add a flag.",
        "orbit.role": CODER,
        "orbit.runtime": "claude",
        "gen_ai.request.model": "opus",
        "orbit.trigger": "manual",
        "orbit.task.fix_attempts": 0,
        "orbit.task.wait_ms": 0.0,
        "orbit.task.check_runs": 0,
        "orbit.verify.result": "passed",
        "orbit.merge.result": "merged",
        "gen_ai.usage.input_tokens": 10,
        "gen_ai.usage.output_tokens": 2,
        "gen_ai.usage.cache_creation.input_tokens": 3,
        "gen_ai.usage.cache_read.input_tokens": 4,
        "orbit.cost.usd": 0.75,
        "orbit.task.status": "completed",
    }
    assert task.status.status_code is StatusCode.UNSET


async def test_a_task_that_waited_for_a_coder_has_its_wait_as_a_child() -> None:
    traced = Traced()
    session = traced_session(traced, claims_done)
    await greeted(session)  # Served, it shares the control plane's slots.
    slots = session.coder_slots
    slots.cap(1)
    assert await slots.acquire("elsewhere", "session")
    await send(session, AddTask(task=TaskSpec(prompt="a")))
    await send(session, RunTasks())
    while slots.waiting == 0:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.01)
    slots.release()
    await events_until(session, idle())

    task = traced.task()
    [wait] = traced.spans("orbit.task.wait")
    assert child_of(wait, task)
    assert dict(attributes(wait)) == {"orbit.wait.position": 1, "orbit.wait.slots_busy": 1}
    wait_ms = attributes(task)["orbit.task.wait_ms"]
    assert isinstance(wait_ms, float) and wait_ms > 0
    assert attributes(task)["orbit.task.status"] == "completed"


async def test_a_step_that_passes_after_a_fix_counts_one_fix_attempt() -> None:
    traced = Traced()
    session = traced_session(traced, claims_twice, verification=verified(Steps(False, True)))
    await asyncio.wait_for(run(session), 5)

    task = traced.task()
    assert attributes(task)["orbit.task.fix_attempts"] == 1
    assert attributes(task)["orbit.verify.result"] == "passed"
    assert attributes(task)["orbit.verify.failed_step"] == STEP.name
    assert [step["result"] for step in step_events(task)] == ["failed", "passed"]


async def test_a_task_that_gives_up_says_why_and_has_its_event() -> None:
    traced = Traced()
    session = traced_session(traced, gives_up)
    await asyncio.wait_for(run(session), 5)

    task = traced.task()
    assert attributes(task)["orbit.task.status"] == "gave_up"
    assert attributes(task)["orbit.task.reason"] == "Gave up: The task contradicts the code."
    assert [event.name for event in task.events] == ["orbit.task.gave_up"]
    assert task.status.status_code is StatusCode.UNSET


async def test_a_review_of_landed_work_records_nothing_on_the_tasks_span(
    caplog: pytest.LogCaptureFixture,
) -> None:
    traced = Traced()
    session, _, _ = session_for(
        claims_done,
        reviewers=(gives_up,),
        tree=FakeTree(),
        review=True,
        telemetry=traced.telemetry,
    )
    with caplog.at_level(logging.WARNING, logger="opentelemetry"):
        await start(session)
        await asyncio.wait_for(settle(session), 10)

    task = traced.task()
    assert attributes(task)["orbit.task.status"] == "completed"
    assert [event.name for event in task.events] == []
    assert caplog.records == []


async def test_a_task_that_errors_has_error_status_and_an_exception_event() -> None:
    traced = Traced()
    session = traced_session(traced, chats)
    await asyncio.wait_for(run(session), 5)

    task = traced.task()
    reason = "Error: agent-1 exited without calling get_task."
    assert attributes(task)["orbit.task.status"] == "error"
    assert attributes(task)["orbit.task.reason"] == reason
    assert task.status.status_code is StatusCode.ERROR
    assert task.status.description == reason
    [exception] = task.events
    assert exception.name == "exception"
    assert (exception.attributes or {})["exception.message"] == reason


async def test_a_task_the_user_stops_ends_cancelled() -> None:
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

    task = traced.task()
    assert attributes(task)["orbit.task.status"] == "cancelled"
    [run_span] = traced.spans("orbit.task.run")
    assert child_of(run_span, task)


async def test_a_task_closed_as_it_runs_ends_cancelled() -> None:
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
    await session.close()

    assert attributes(traced.task())["orbit.task.status"] == "cancelled"


async def test_no_attribute_holds_the_tasks_prompt() -> None:
    traced = Traced()
    session = traced_session(traced, claims_done)
    prompt = "Add a flag.\n\nThe secret body of the prompt."
    await asyncio.wait_for(run(session, TaskSpec(prompt=prompt)), 5)

    values = [str(value) for value in attributes(traced.task()).values()]
    assert attributes(traced.task())["orbit.task.title"] == "Add a flag."
    assert not any("secret body" in value for value in values)


async def test_a_task_naming_no_model_is_traced_with_none_as_it_is_launched() -> None:
    traced = Traced()
    roles = {**ROLES, CODER: ROLES[CODER].model_copy(update={"model": "sonnet"})}
    session, _, launcher = session_for(claims_done, telemetry=traced.telemetry, roles=roles)
    await asyncio.wait_for(run(session, TaskSpec(prompt="a")), 5)

    [launch] = launcher.launches
    assert launch.settings.model is None
    assert "gen_ai.request.model" not in attributes(traced.task())


async def test_a_task_naming_a_model_is_traced_with_it() -> None:
    traced = Traced()
    roles = {**ROLES, CODER: ROLES[CODER].model_copy(update={"model": "sonnet"})}
    session, _, launcher = session_for(claims_done, telemetry=traced.telemetry, roles=roles)
    await asyncio.wait_for(run(session, TaskSpec(prompt="a", settings=Settings(model="opus"))), 5)

    [launch] = launcher.launches
    assert launch.settings.model == "opus"
    assert attributes(traced.task())["gen_ai.request.model"] == "opus"


async def test_a_task_orbit_queues_in_a_role_naming_a_model_is_traced_with_it() -> None:
    traced = Traced()
    coder = ROLES[CODER].model_copy(update={"model": "sonnet"})
    session, _, launcher = session_for(
        claims_done, telemetry=traced.telemetry, roles={**ROLES, CODER: coder}
    )
    await asyncio.wait_for(run(session, role_task(CODER, coder, "a")), 5)

    [launch] = launcher.launches
    assert launch.settings.model == "sonnet"
    assert attributes(traced.task())["gen_ai.request.model"] == "sonnet"


async def test_a_task_of_no_known_role_and_no_model_names_no_model() -> None:
    traced = Traced()
    session = traced_session(traced)
    await asyncio.wait_for(run(session, TaskSpec(prompt="a", role="nobody")), 5)

    task = traced.task()
    assert "gen_ai.request.model" not in attributes(task)
    assert attributes(task)["orbit.role"] == "nobody"


async def test_the_agents_check_runs_are_counted() -> None:
    traced = Traced()
    session = traced_session(traced, runs_checks, verification=verified(Steps(True, True, True)))
    await asyncio.wait_for(run(session), 5)

    task = traced.task()
    assert attributes(task)["orbit.task.check_runs"] == 2
    assert len(step_events(task)) == 1  # The agent's own check runs aren't the task's verification.


async def test_a_merge_conflict_is_its_result_and_an_event() -> None:
    traced = Traced()
    tree = FakeTree(Merge("conflict", "The base branch conflicts."))
    session = traced_session(traced, claims_done, tree=tree)
    await asyncio.wait_for(run(session), 5)

    task = traced.task()
    assert attributes(task)["orbit.merge.result"] == "conflict"
    assert [event.name for event in task.events] == ["orbit.merge.conflict"]


async def test_verification_with_no_steps_is_skipped() -> None:
    traced = Traced()
    session = traced_session(traced, claims_done, verification=Verification((), Steps()))
    await asyncio.wait_for(run(session), 5)

    assert attributes(traced.task())["orbit.verify.result"] == "skipped"


async def test_a_fix_task_is_its_own_trace_naming_the_task_it_fixes() -> None:
    traced = Traced()
    session = traced_session(
        traced,
        keeps_claiming,
        claims_done,
        verification=verified(Steps(False, False, True)),
        limits=Limits(max_attempts=1),
    )
    await asyncio.wait_for(run(session), 5)

    first, fix = traced.spans("orbit.task")
    assert attributes(first)["orbit.task.status"] == "unverified"
    assert attributes(first)["orbit.verify.result"] == "failed"
    assert attributes(first)["orbit.verify.failed_step"] == STEP.name
    assert "orbit.task.fixes" not in attributes(first)
    assert attributes(fix)["orbit.task.fixes"] == "1"
    assert attributes(fix)["orbit.task.status"] == "completed"
    assert fix.parent is None and fix.context is not None and first.context is not None
    assert fix.context.trace_id != first.context.trace_id


def test_a_bug_escaping_a_task_is_recorded_once() -> None:
    traced = Traced()
    board = TaskBoard()
    task_id = board.add(TaskSpec(prompt="a"))
    tracing = TaskTracing(traced.telemetry, "workspace", lambda: "session")

    with pytest.raises(RuntimeError), tracing.task(board, task_id, None):
        board.finish(task_id, "error", "Orbit hit a bug.")
        raise RuntimeError("broken")

    task = traced.task()
    assert task.status.status_code is StatusCode.ERROR
    [exception] = task.events
    assert (exception.attributes or {})["exception.message"] == "broken"
    assert attributes(task)["orbit.task.status"] == "error"


def test_a_task_removed_as_it_ends_has_no_ending() -> None:
    traced = Traced()
    board = TaskBoard()
    task_id = board.add(TaskSpec(prompt="a", settings=Settings(model="opus")))
    tracing = TaskTracing(traced.telemetry, "workspace", lambda: "session")

    with tracing.task(board, task_id, "opus"):
        board.remove(task_id)

    task = traced.task()
    assert "orbit.task.status" not in attributes(task)
    assert attributes(task)["orbit.workspace.id"] == "workspace"
