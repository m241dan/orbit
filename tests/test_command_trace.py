"""The ``orbit.command`` span each command is handled in: what it is, where it's for, who sent it
and how it came out, under the span its envelope carries, if any, else starting its own trace."""

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode
from opentelemetry.util.types import AttributeValue

from orbit.agent import ControlPlane, WorkspaceHost
from orbit.agent.application.command_trace import COMMAND_SPAN
from orbit.protocol import (
    INTERNAL,
    INVALID,
    UNKNOWN,
    AddTask,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    EventEnvelope,
    RunTasks,
    TaskSpec,
    Welcome,
)
from tests.agent_fakes import (
    COMMAND_ID,
    HELLO,
    HELLO_ID,
    WORKSPACE,
    FakeStore,
    close,
    frontend,
    greeted,
    host_of,
    queued_envelopes,
    send,
    session_for,
)
from tests.telemetry_helpers import Traced, traceparent_of

TASK = TaskSpec(prompt="Add a flag.")

_TYPE = "orbit.command.type"


def _plane(traced: Traced) -> ControlPlane:
    """A control plane serving a host, both traced by ``traced``."""
    host, _, _ = session_for(telemetry=traced.telemetry)
    return ControlPlane([host], telemetry=traced.telemetry)


def _command(traced: Traced, kind: str) -> ReadableSpan:
    """The one command span of a command of type ``kind``."""
    [span] = [span for span in traced.spans(COMMAND_SPAN) if _attributes(span)[_TYPE] == kind]
    return span


def _attributes(span: ReadableSpan) -> dict[str, AttributeValue]:
    return dict(span.attributes or {})


def _answer(sent: list[EventEnvelope], command_id: str) -> EventEnvelope:
    """The envelope of the outcome of the command sent as ``command_id``."""
    outcomes = CommandAccepted | CommandRefused | Welcome
    [answer] = [
        envelope
        for envelope in sent
        if isinstance(envelope.payload, outcomes) and envelope.payload.command_id == command_id
    ]
    return answer


def _trace_id(span: ReadableSpan) -> int:
    assert span.context is not None
    return int(span.context.trace_id)


async def test_a_command_is_one_span_naming_it_where_its_for_and_its_client_and_accepted() -> None:
    traced = Traced()
    plane = _plane(traced)
    await send(plane, AddTask(task=TASK))
    sent = queued_envelopes(plane)
    session_id = host_of(plane).saved_session.id
    await close(plane)

    command = _command(traced, "add_task")
    assert _attributes(command) == {
        _TYPE: "add_task",
        "orbit.workspace.id": WORKSPACE,
        "orbit.session.id": session_id,
        "orbit.client": "orbit-tests",
        "orbit.command.outcome": "accepted",
    }
    assert command.status.status_code is StatusCode.UNSET
    assert _answer(sent, COMMAND_ID).traceparent == traceparent_of(command)


async def test_a_hello_welcomed_is_accepted_naming_its_client_and_its_welcome_carries_it() -> None:
    traced = Traced()
    plane = _plane(traced)
    await frontend(plane).send(CommandEnvelope(command_id=HELLO_ID, payload=HELLO))
    sent = queued_envelopes(plane)
    await close(plane)

    hello = _command(traced, "hello")
    assert _attributes(hello) == {
        _TYPE: "hello",
        "orbit.client": "orbit-tests",
        "orbit.command.outcome": "accepted",
    }
    assert _answer(sent, HELLO_ID).traceparent == traceparent_of(hello)


async def test_a_refused_command_records_its_code_and_its_refusal_carries_it() -> None:
    traced = Traced()
    plane = _plane(traced)
    await frontend(plane).send(CommandEnvelope(command_id=COMMAND_ID, payload=AddTask(task=TASK)))
    sent = queued_envelopes(plane)
    await close(plane)

    command = _command(traced, "add_task")
    assert _attributes(command) == {
        _TYPE: "add_task",
        "orbit.command.outcome": "refused",
        "orbit.command.code": INVALID,
    }
    assert command.status.status_code is StatusCode.UNSET
    assert _answer(sent, COMMAND_ID).traceparent == traceparent_of(command)


async def test_a_command_records_the_workspace_and_session_its_envelope_names() -> None:
    traced = Traced()
    plane = _plane(traced)
    await greeted(plane)
    envelope = CommandEnvelope(
        command_id=COMMAND_ID, workspace="elsewhere", session="other", payload=RunTasks()
    )
    await frontend(plane).send(envelope)
    await close(plane)

    command = _attributes(_command(traced, "run_tasks"))
    assert (command["orbit.workspace.id"], command["orbit.session.id"]) == ("elsewhere", "other")
    assert command["orbit.command.code"] == UNKNOWN


async def test_a_command_whose_handling_fails_has_the_error_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    traced = Traced()
    store = FakeStore()
    store.list_failure = ValueError("the disk is gone")
    plane = ControlPlane([WorkspaceHost(store=store)], telemetry=traced.telemetry)
    await frontend(plane).send(CommandEnvelope(command_id=HELLO_ID, payload=HELLO))
    await close(plane)

    hello = _command(traced, "hello")
    assert _attributes(hello)["orbit.command.outcome"] == "refused"
    assert _attributes(hello)["orbit.command.code"] == INTERNAL
    assert hello.status.status_code is StatusCode.ERROR
    assert hello.status.description == "Orbit failed to handle Hello: the disk is gone"
    assert [event.name for event in hello.events] == ["exception"]


async def test_a_command_carrying_a_span_from_another_process_is_its_child() -> None:
    traced, frontend_traced = Traced(), Traced()
    plane = _plane(traced)
    await greeted(plane)
    with frontend_traced.telemetry.span("orbit.tui.action"):
        envelope = frontend_traced.telemetry.carry(
            CommandEnvelope(command_id=COMMAND_ID, payload=AddTask(task=TASK))
        )
    await frontend(plane).send(envelope)
    await close(plane)

    command = _command(traced, "add_task")
    [action] = frontend_traced.spans("orbit.tui.action")
    assert command.parent is not None and action.context is not None
    assert command.parent.is_remote
    assert command.parent.span_id == action.context.span_id
    assert _trace_id(command) == _trace_id(action)


async def test_a_command_carrying_no_trace_context_starts_its_own_trace() -> None:
    traced = Traced()
    plane = _plane(traced)
    with traced.telemetry.span("orbit.connection"):
        await send(plane, AddTask(task=TASK))
    await close(plane)

    command = _command(traced, "add_task")
    [connection] = traced.spans("orbit.connection")
    assert command.parent is None
    assert _trace_id(command) != _trace_id(connection)
