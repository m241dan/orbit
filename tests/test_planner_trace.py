"""Each planner turn's ``orbit.planner.turn`` span: a child of the ``SendMessage`` command that
asked for it, with its model, what it cost, the drafts it proposed and how it ended."""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode

from orbit.agent import ControlPlane
from orbit.agent.application.command_trace import COMMAND_SPAN
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.planner import PlannerChat
from orbit.agent.application.planner_trace import TURN_SPAN
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.roles import Role
from orbit.protocol import (
    PLANNER,
    CancelTurn,
    CommandRefused,
    Event,
    SendMessage,
    Settings,
    TurnEndReason,
    TurnFinished,
    TurnStarted,
    Usage,
)
from tests.agent_fakes import (
    ROLES,
    WORKSPACE,
    Call,
    Script,
    ScriptedLauncher,
    agents_for,
    chats,
    close,
    events_until,
    gated_planner,
    hangs,
    idle,
    proposes_two,
    send,
    session_for,
)
from tests.telemetry_helpers import Traced
from tests.waiting import eventually

ON_OPUS = {**ROLES, PLANNER: ROLES[PLANNER].model_copy(update={"model": "opus"})}
"""The roles, with the planner's on Opus."""

TURN_COST = Usage(
    input_tokens=10,
    output_tokens=2,
    cache_creation_input_tokens=3,
    cache_read_input_tokens=4,
    cost_usd=0.5,
)

_END_REASON = "orbit.planner.end_reason"


def attributes(span: ReadableSpan) -> Mapping[str, Any]:
    assert span.attributes is not None
    return span.attributes


def child_of(child: ReadableSpan, parent: ReadableSpan) -> bool:
    assert child.parent is not None and parent.context is not None
    return bool(child.parent.span_id == parent.context.span_id)


def traced_plane(
    traced: Traced, *turns: Script, roles: Mapping[str, Role] = ON_OPUS
) -> tuple[ControlPlane, ScriptedLauncher]:
    """A control plane serving a host whose planner plays ``turns`` with ``roles``, both traced
    by ``traced``, and the launcher playing them."""
    host, _, launcher = session_for(telemetry=traced.telemetry, roles=roles)
    launcher.planner_turns = list(turns)
    return ControlPlane([host], telemetry=traced.telemetry), launcher


async def planned(plane: ControlPlane) -> None:
    """Prompt ``plane``'s planner and wait for its turn to end."""
    await send(plane, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(plane, idle("planner"))


def messages(traced: Traced) -> list[ReadableSpan]:
    """The spans of the ``SendMessage`` commands handled."""
    commands = traced.spans(COMMAND_SPAN)
    return [span for span in commands if attributes(span)["orbit.command.type"] == "send_message"]


async def fails(call: Call) -> TurnEndReason:
    raise RuntimeError("Could not start 'claude'.")


async def test_a_turn_is_a_child_of_its_message_with_its_model_cost_and_end_reason() -> None:
    traced = Traced()
    plane, launcher = traced_plane(traced, chats)
    launcher.planner_usages = [TURN_COST]
    await planned(plane)
    await close(plane)

    [message] = messages(traced)
    [turn] = traced.spans(TURN_SPAN)
    assert child_of(turn, message)
    assert dict(attributes(turn)) == {
        "orbit.workspace.id": WORKSPACE,
        "orbit.session.id": "session-1",
        "gen_ai.request.model": "opus",
        "gen_ai.usage.input_tokens": 10,
        "gen_ai.usage.output_tokens": 2,
        "gen_ai.usage.cache_creation.input_tokens": 3,
        "gen_ai.usage.cache_read.input_tokens": 4,
        "orbit.cost.usd": 0.5,
        "orbit.planner.drafts": 0,
        _END_REASON: "completed",
    }
    assert turn.status.status_code is StatusCode.UNSET


async def test_a_turn_on_the_clis_default_model_names_none() -> None:
    traced = Traced()
    plane, _ = traced_plane(traced, chats, roles=ROLES)
    await planned(plane)
    await close(plane)

    [turn] = traced.spans(TURN_SPAN)
    assert "gen_ai.request.model" not in attributes(turn)
    assert attributes(turn)["orbit.cost.usd"] == 0.0


async def test_each_turn_counts_the_drafts_it_proposed() -> None:
    traced = Traced()
    plane, _ = traced_plane(traced, proposes_two, chats)
    await planned(plane)
    await planned(plane)
    await close(plane)

    turns = sorted(traced.spans(TURN_SPAN), key=lambda span: span.start_time or 0)
    assert [attributes(turn)["orbit.planner.drafts"] for turn in turns] == [2, 0]


async def test_a_cancelled_turn_ends_cancelled() -> None:
    traced = Traced()
    plane, _ = traced_plane(traced, hangs)
    await send(plane, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(plane, lambda e: isinstance(e, TurnStarted))
    await send(plane, CancelTurn(lane="planner"))
    await events_until(plane, idle("planner"))
    await close(plane)

    [turn] = traced.spans(TURN_SPAN)
    assert attributes(turn)[_END_REASON] == "cancelled"
    assert turn.status.status_code is StatusCode.UNSET


async def test_a_turn_that_fails_has_the_error_status_and_ends_in_error() -> None:
    traced = Traced()
    plane, _ = traced_plane(traced, fails)
    await planned(plane)
    await close(plane)

    [turn] = traced.spans(TURN_SPAN)
    assert attributes(turn)[_END_REASON] == "error"
    assert turn.status.status_code is StatusCode.ERROR
    assert turn.status.description == "Could not start 'claude'."
    assert [event.name for event in turn.events] == ["exception"]


async def test_a_turn_the_cli_ends_in_error_has_the_error_status_with_its_message() -> None:
    traced = Traced()
    plane, launcher = traced_plane(traced, chats)
    launcher.planner_errors = ["API Error: 529 Overloaded"]
    await planned(plane)
    await close(plane)

    [turn] = traced.spans(TURN_SPAN)
    assert attributes(turn)[_END_REASON] == "error"
    assert turn.status.status_code is StatusCode.ERROR
    assert turn.status.description == "API Error: 529 Overloaded"


async def test_a_message_refused_as_busy_starts_no_turn() -> None:
    traced = Traced()
    gate = asyncio.Event()
    plane, _ = traced_plane(traced, gated_planner(gate))
    await send(plane, SendMessage(agent=PLANNER, text="Plan."))
    await send(plane, SendMessage(agent=PLANNER, text="More."))
    await events_until(plane, lambda e: isinstance(e, CommandRefused))
    gate.set()
    await events_until(plane, idle("planner"))
    await close(plane)

    first, _ = sorted(messages(traced), key=lambda span: span.start_time or 0)
    [turn] = traced.spans(TURN_SPAN)
    assert child_of(turn, first)


async def test_a_turn_cancelled_again_as_it_ends_still_ends_cancelled() -> None:
    traced = Traced()
    agents, launcher = agents_for()
    launcher.planner_turns = [hangs]
    ending = asyncio.Event()

    async def publish(event: Event) -> None:
        if isinstance(event, TurnFinished):
            ending.set()
            await asyncio.Event().wait()

    chat = PlannerChat(AgentDesk(agents, publish), Tasks(TaskBoard(), publish), lambda: ())

    async def answer() -> TurnEndReason:
        with traced.telemetry.span(TURN_SPAN):
            return await chat.answer("Plan.", Settings())

    answering = asyncio.create_task(answer())
    await eventually(lambda: launcher.launches, poll=0)
    answering.cancel()
    await ending.wait()
    answering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await answering

    [turn] = traced.spans(TURN_SPAN)
    assert attributes(turn)[_END_REASON] == "cancelled"
