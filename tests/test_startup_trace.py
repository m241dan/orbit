"""The startup check's ``orbit.startup_check`` span: a root for each run, with an
``orbit.verify.step`` child for each step it runs."""

import asyncio
from collections.abc import Mapping
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode

from orbit.agent import Setup, WorkspaceHost
from orbit.agent.application.agent_trace import STEP_SPAN
from orbit.agent.application.ports import TreeFailed
from orbit.agent.application.startup_trace import STARTUP_SPAN
from orbit.protocol import (
    CODER,
    AgentExited,
    CancelAgent,
    EventEnvelope,
    StartupChecked,
    StepState,
    VerificationChanged,
)
from tests.agent_fakes import (
    ROLES,
    FakeTree,
    Steps,
    agent_exited,
    checked,
    events_until,
    frontend,
    greeted,
    payloads,
    send,
    session_for,
)
from tests.telemetry_helpers import Traced, traceparent_of


def attributes(span: ReadableSpan) -> Mapping[str, Any]:
    return dict(span.attributes or {})


def startup(traced: Traced) -> ReadableSpan:
    """The one startup check's span."""
    [span] = traced.spans(STARTUP_SPAN)
    return span


def steps_of(traced: Traced, parent: ReadableSpan) -> list[ReadableSpan]:
    """The step spans that are children of ``parent``, in the order they started."""
    assert parent.context is not None
    children = [
        span
        for span in traced.spans(STEP_SPAN)
        if span.parent is not None and span.parent.span_id == parent.context.span_id
    ]
    return sorted(children, key=lambda span: span.start_time or 0)


def traced_session(traced: Traced, steps: Steps, tree: FakeTree) -> WorkspaceHost:
    """A session checking ``steps`` at startup in ``tree``, traced by ``traced``."""
    session, _, _ = session_for(
        verification=checked(steps), tree=tree, check_at_startup=True, telemetry=traced.telemetry
    )
    return session


async def checked_with(steps: Steps, tree: FakeTree | None = None) -> Traced:
    """What tracing a session's startup check of ``steps`` in ``tree`` recorded."""
    traced = Traced()
    session = traced_session(traced, steps, tree or FakeTree())
    await events_until(session, agent_exited("startup-check"))
    await session.close()
    return traced


async def test_a_passing_check_is_a_root_span_with_a_child_for_each_step() -> None:
    traced = await checked_with(Steps(True, True))

    span = startup(traced)
    # The first Attach's command span is current as the check starts; the check's isn't its child.
    assert span.parent is None
    assert attributes(span) == {
        "orbit.workspace.id": "unregistered",
        "orbit.verify.result": "passed",
        "orbit.startup_check.fix_added": False,
    }
    steps = steps_of(traced, span)
    assert [attributes(step) for step in steps] == [
        {"orbit.verify.step.name": "Lint", "orbit.verify.step.result": "passed"},
        {"orbit.verify.step.name": "Unit tests", "orbit.verify.step.result": "passed"},
    ]


async def test_a_failing_check_names_the_step_that_failed_and_the_fix_it_added() -> None:
    traced = await checked_with(Steps(False))

    span = startup(traced)
    assert attributes(span) == {
        "orbit.workspace.id": "unregistered",
        "orbit.verify.result": "failed",
        "orbit.verify.failed_step": "Lint",
        "orbit.startup_check.fix_added": True,
    }
    [step] = steps_of(traced, span)
    assert attributes(step)["orbit.verify.step.result"] == "failed"


async def test_a_failing_check_adds_no_fix_while_the_coder_is_disabled() -> None:
    traced = Traced()
    coder = ROLES[CODER].model_copy(update={"enabled": False})
    session, _, _ = session_for(
        verification=checked(Steps(False)),
        tree=FakeTree(),
        check_at_startup=True,
        telemetry=traced.telemetry,
        roles={**ROLES, CODER: coder},
    )
    await events_until(session, agent_exited("startup-check"))
    await session.close()

    span = startup(traced)
    assert attributes(span)["orbit.verify.result"] == "failed"
    assert attributes(span)["orbit.startup_check.fix_added"] is False


async def test_the_trees_setup_is_a_step_of_its_own() -> None:
    tree = FakeTree()
    tree.setup = Setup(command=("uv", "sync"))

    traced = await checked_with(Steps(True, True, True), tree)

    names = [
        attributes(step)["orbit.verify.step.name"] for step in steps_of(traced, startup(traced))
    ]
    assert names == ["setup", "Lint", "Unit tests"]


async def test_a_cancelled_check_ends_its_span_cancelled_with_its_step_cut_short() -> None:
    traced = Traced()
    steps = Steps(True)
    steps.gate = asyncio.Event()
    session = traced_session(traced, steps, FakeTree())
    running = VerificationChanged(
        steps=(StepState(name="Lint", status="running"), StepState(name="Unit tests")),
        title="Startup check",
    )
    async for event in payloads(session):
        if event == running:
            await send(session, CancelAgent(agent_id="startup-check"))
        if isinstance(event, AgentExited):
            break
    await session.close()

    span = startup(traced)
    assert attributes(span) == {
        "orbit.workspace.id": "unregistered",
        "orbit.verify.result": "cancelled",
        "orbit.startup_check.fix_added": False,
    }
    assert span.status.status_code is StatusCode.UNSET
    [step] = steps_of(traced, span)
    assert attributes(step) == {"orbit.verify.step.name": "Lint"}


async def test_a_check_git_cannot_set_up_has_the_spans_error_and_no_result() -> None:
    class Broken(FakeTree):
        async def snapshot(self, commit: str) -> str:
            raise TreeFailed("git worktree failed: no commits")

    traced = await checked_with(Steps(), Broken())

    span = startup(traced)
    assert "orbit.verify.result" not in attributes(span)
    assert span.status.status_code is StatusCode.ERROR
    message = "The startup check couldn't run: git worktree failed: no commits"
    assert span.status.description == message
    assert [event.name for event in span.events] == ["exception"]
    assert steps_of(traced, span) == []


async def test_the_events_the_check_publishes_carry_its_trace_context() -> None:
    traced = Traced()
    session = traced_session(traced, Steps(True, True), FakeTree())
    await greeted(session)
    sent: list[EventEnvelope] = []
    async for envelope in frontend(session).events():
        sent.append(envelope)
        if agent_exited("startup-check")(envelope.payload):
            break
    await session.close()

    [result] = [envelope for envelope in sent if isinstance(envelope.payload, StartupChecked)]
    assert result.traceparent == traceparent_of(startup(traced))
