"""The user's actions as the TUI traces them: each that sends commands is one
``orbit.tui.action`` span, started as it sends the first, whose trace context every command it
sends carries; commands sent outside an action carry none."""

import asyncio

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode

from orbit.protocol import PLANNER_LANE, CancelTurn, Command, CommandEnvelope, RunTasks
from orbit.tracing import NoTelemetry, Telemetry
from orbit.tui.view.actions import ACTION_SPAN, action, carrying
from orbit.tui.view.commands import Send
from tests.telemetry_helpers import Traced, traceparent_of


class Wire:
    """Where the envelopes go: every one sent, in turn."""

    def __init__(self) -> None:
        self.sent: list[CommandEnvelope] = []

    async def send(self, envelope: CommandEnvelope) -> None:
        self.sent.append(envelope)


def envelope(command: Command, command_id: str = "c1") -> CommandEnvelope:
    return CommandEnvelope(command_id=command_id, payload=command)


def wired(telemetry: Telemetry) -> tuple[Send, Wire]:
    """What sends to a wire, carrying the action's trace context with ``telemetry``, and the
    wire."""
    wire = Wire()
    return carrying(wire.send, telemetry), wire


def action_of(span: ReadableSpan) -> object:
    return (span.attributes or {}).get("orbit.action")


async def test_a_command_sent_in_an_action_carries_its_span_named_for_it() -> None:
    traced = Traced()
    send, wire = wired(traced.telemetry)
    with action("run_tasks"):
        await send(envelope(RunTasks()))
    [span] = traced.spans(ACTION_SPAN)
    assert action_of(span) == "run_tasks"
    assert [sent.traceparent for sent in wire.sent] == [traceparent_of(span)]
    assert wire.sent[0].payload == RunTasks()


async def test_an_action_sending_several_commands_is_one_span_they_all_carry() -> None:
    traced = Traced()
    send, wire = wired(traced.telemetry)
    with action("stop_planner"):
        await send(envelope(RunTasks(), "c1"))
        await send(envelope(CancelTurn(lane=PLANNER_LANE), "c2"))
    [span] = traced.spans(ACTION_SPAN)
    assert [sent.traceparent for sent in wire.sent] == [traceparent_of(span)] * 2


async def test_an_action_sending_nothing_starts_no_span() -> None:
    traced = Traced()
    with action("run_tasks"):
        pass
    assert traced.spans(ACTION_SPAN) == []


async def test_a_command_sent_outside_any_action_is_sent_unchanged() -> None:
    traced = Traced()
    send, wire = wired(traced.telemetry)
    await send(envelope(RunTasks()))
    assert wire.sent == [envelope(RunTasks())]
    assert traced.spans(ACTION_SPAN) == []


async def test_a_command_sent_once_its_action_is_over_is_sent_unchanged() -> None:
    traced = Traced()
    send, wire = wired(traced.telemetry)
    release = asyncio.Event()

    async def later() -> None:
        await release.wait()
        await send(envelope(RunTasks()))

    with action("run_tasks"):
        sending = asyncio.create_task(later())  # Started in the action, so it shares it.
    release.set()
    await sending
    assert wire.sent == [envelope(RunTasks())]
    assert traced.spans(ACTION_SPAN) == []


async def test_actions_one_inside_another_are_each_their_own_span() -> None:
    traced = Traced()
    send, wire = wired(traced.telemetry)
    with action("pick_workspace"):
        await send(envelope(RunTasks(), "c1"))
        with action("browse"):
            await send(envelope(RunTasks(), "c2"))
        await send(envelope(RunTasks(), "c3"))
    spans = {action_of(span): span for span in traced.spans(ACTION_SPAN)}
    picking, browsing = traceparent_of(spans["pick_workspace"]), traceparent_of(spans["browse"])
    assert [sent.traceparent for sent in wire.sent] == [picking, browsing, picking]


async def test_an_error_escaping_an_action_fails_its_span() -> None:
    traced = Traced()
    send, _ = wired(traced.telemetry)
    with pytest.raises(RuntimeError), action("run_tasks"):
        await send(envelope(RunTasks()))
        raise RuntimeError("boom")
    [span] = traced.spans(ACTION_SPAN)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "boom"
    assert [event.name for event in span.events] == ["exception"]


async def test_an_error_escaping_an_action_before_it_sends_starts_no_span() -> None:
    traced = Traced()
    with pytest.raises(RuntimeError), action("run_tasks"):
        raise RuntimeError("boom")
    assert traced.spans(ACTION_SPAN) == []


async def test_without_telemetry_a_command_sent_in_an_action_is_sent_unchanged() -> None:
    send, wire = wired(NoTelemetry())
    with action("run_tasks"):
        await send(envelope(RunTasks()))
    assert wire.sent == [envelope(RunTasks())]
