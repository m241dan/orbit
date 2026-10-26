"""A role consulting past its hourly limit: the consultation is held and every frontend asked;
allowed, it runs; allowed for the hour, the role consults freely meanwhile; denied, or expired
after an hour with no frontend connected, the asker hears it was interrupted."""

import asyncio
from collections.abc import Callable

from orbit.agent import WorkspaceHost
from orbit.agent.application.consultations import held_note
from orbit.agent.application.conversation_seats import ConversationSeats
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.planner_seat import SeatHooks, Timer
from orbit.agent.application.ports import Reply, StoreFailed
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.consult_limits import HOUR
from orbit.agent.domain.roles import Role
from orbit.agent.domain.sessions import SessionRecord
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.protocol import (
    PLANNER,
    UNKNOWN,
    ActivateSession,
    ApprovalDecided,
    ApprovalDecision,
    ApprovalRequested,
    CommandRefused,
    ConsultationAnswered,
    DecideApproval,
    Event,
    NewSession,
    SendMessage,
    Settings,
    TurnEndReason,
    TurnFinished,
)
from tests.agent_fakes import (
    ROLES,
    Call,
    FakeStore,
    Script,
    ScriptedLauncher,
    Ticks,
    agents_for,
    chats,
    events_until,
    halt_of,
    of_type,
    refused,
    resume_storage,
    send,
    session_for,
    snapshot_of,
    sole,
)
from tests.session_builders import opened, saved, store_with
from tests.waiting import eventually

EXPERIMENTER = "experimenter"
EXPERIMENTER_ROLE = ROLES[PLANNER].model_copy(
    update={"instructions": "You experiment.", "lane": EXPERIMENTER}
)
CONSULTING_PLANNER = ROLES[PLANNER].model_copy(update={"consults": (EXPERIMENTER,)})
CONSULTING = {**ROLES, PLANNER: CONSULTING_PLANNER, EXPERIMENTER: EXPERIMENTER_ROLE}
"""The built-in roles, with a planner consulting an experimenter in its own lane."""

ASKED = "Asked the experimenter (consultation {}); its reply will arrive as a message."


def limited(limit: int) -> dict[str, Role]:
    """The roles of a planner that may consult the experimenter ``limit`` times an hour."""
    planner = CONSULTING_PLANNER.model_copy(update={"consult_limit_per_hour": limit})
    return {**CONSULTING, PLANNER: planner}


LIMITED = limited(1)


def requested(number: int, title: str, asked: int = 1) -> ApprovalRequested:
    return ApprovalRequested(
        consultation=number, asker=PLANNER, consultant=EXPERIMENTER, title=title, asked=asked
    )


def held(number: int) -> Reply:
    return Reply(held_note(EXPERIMENTER, number))


def consulting(heard: list[Reply], gate: asyncio.Event, *after: str) -> Script:
    """A turn consulting with "Check X" and "Check Y", then, once ``gate`` is set, with each of
    ``after``, noting what it hears in ``heard``."""

    async def turn(call: Call) -> TurnEndReason:
        for message in ("Check X", "Check Y", *after):
            if message in after:
                await gate.wait()
            heard.append(await call("consult", role=EXPERIMENTER, message=message))
        return "completed"

    return turn


def answering(number: int) -> Callable[[Event], bool]:
    """Stops at consultation ``number``'s ConsultationAnswered."""
    return lambda e: isinstance(e, ConsultationAnswered) and e.consultation == number


async def holding(
    *after: str, roles: dict[str, Role] = LIMITED
) -> tuple[WorkspaceHost, ScriptedLauncher, list[Reply], asyncio.Event]:
    """A session whose planner consulted twice, the second past its limit, so it's held; its
    turn consults with each of ``after`` once the gate returned is set."""
    heard: list[Reply] = []
    gate = asyncio.Event()
    session, _, launcher = session_for(roles=roles)
    launcher.planner_turns = [consulting(heard, gate, *after), chats, chats, chats, chats]
    launcher.planner_replies = ["Asking.", "X is 3", "Y is 4", "Z is 5", "Thanks."]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, of_type(ApprovalRequested))
    return session, launcher, heard, gate


async def test_consultations_under_the_limit_run_and_one_past_it_is_held() -> None:
    session, launcher, heard, _ = await holding()
    snapshot = await snapshot_of(session)
    await eventually(lambda: len(launcher.launches) >= 2)
    await session.close()

    assert heard == [Reply(ASKED.format(1)), held(2)]
    assert snapshot.approvals == (requested(2, "Check Y"),)
    consulted = launcher.launches[1]
    assert (consulted.agent.role, consulted.prompt.endswith("Check X")) == (EXPERIMENTER, True)


async def test_a_limit_of_0_holds_none() -> None:
    heard: list[Reply] = []
    gate = asyncio.Event()
    gate.set()
    session, _, launcher = session_for(roles=limited(0))
    launcher.planner_turns = [consulting(heard, gate, "Check Z"), chats, chats, chats, chats]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await eventually(lambda: len(heard) == 3)
    await session.close()

    assert heard == [Reply(ASKED.format(number)) for number in (1, 2, 3)]


async def decided(decision: ApprovalDecision, *after: str) -> tuple[list[Event], list[Reply]]:
    """The events from the user's ``decision`` on the held consultation 2 up to its answer,
    and what the planner heard, consulting with ``after`` once it's decided."""
    session, _, heard, gate = await holding(*after)
    await send(session, DecideApproval(consultation=2, decision=decision))
    events = await events_until(session, answering(2))
    gate.set()
    await eventually(lambda: len(heard) == 2 + len(after))
    await session.close()
    return events, heard


async def test_allowing_runs_the_consultation() -> None:
    events, _ = await decided("allow")
    assert ApprovalDecided(consultation=2, decision="allow") in events
    assert sole([e for e in events if answering(2)(e)], ConsultationAnswered).interrupted is None


async def test_allowing_once_holds_the_next_past_the_limit() -> None:
    _, heard = await decided("allow", "Check Z")
    assert heard[-1] == held(3)


async def test_allowing_for_the_hour_lets_the_role_consult_freely() -> None:
    events, heard = await decided("allow_hour", "Check Z")
    assert ApprovalDecided(consultation=2, decision="allow_hour") in events
    assert heard[-1] == Reply(ASKED.format(3))


async def test_denying_answers_it_as_declined() -> None:
    events, _ = await decided("deny")
    assert ApprovalDecided(consultation=2, decision="deny") in events
    answer = sole([e for e in events if answering(2)(e)], ConsultationAnswered)
    assert (answer.reply, answer.interrupted) == ("", "you declined it")


async def test_the_first_decision_applies_and_a_later_one_is_refused() -> None:
    session, _, _, _ = await holding()
    await send(session, DecideApproval(consultation=2, decision="deny"))
    await send(session, DecideApproval(consultation=2, decision="allow"))
    events = await events_until(session, of_type(CommandRefused))
    snapshot = await snapshot_of(session)
    await session.close()

    assert events[-1] == refused(UNKNOWN, "No consultation 2 is waiting for approval.")
    assert [e.decision for e in events if isinstance(e, ApprovalDecided)] == ["deny"]
    assert snapshot.approvals == ()


class Clock:
    """A clock that sleeping moves on, at once."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


async def nothing() -> None:
    return None


def drained(events: asyncio.Queue[Event]) -> list[Event]:
    taken: list[Event] = []
    while not events.empty():
        taken.append(events.get_nowait())
    return taken


async def test_one_held_with_no_frontend_connected_expires_denied_after_an_hour() -> None:
    clock = Clock()
    agents, launcher = agents_for(roles=LIMITED)
    events: asyncio.Queue[Event] = asyncio.Queue()
    hooks = SeatHooks(nothing, lambda: None, tuple, timer=Timer(clock, clock.sleep))
    seats = ConversationSeats(
        AgentDesk(agents, events.put), Tasks(TaskBoard(), events.put), events, hooks
    )
    heard: list[Reply] = []
    gate = asyncio.Event()
    launcher.planner_turns = [consulting(heard, gate), chats, chats, chats]
    await seats.planner.submit("Plan.", Settings())
    told = "The experimenter's consultation 'Check Y' was interrupted: it expired waiting for"
    await eventually(lambda: any(told in launch.prompt for launch in launcher.launches))
    taken = drained(events)
    gate.set()
    await seats.stop_turns(final=True)
    await seats.close()

    assert heard == [Reply(ASKED.format(1)), held(2)]
    assert clock.now == HOUR
    assert ApprovalDecided(consultation=2, decision="deny") in taken
    answer = sole([e for e in taken if answering(2)(e)], ConsultationAnswered)
    assert answer.interrupted == "it expired waiting for approval"


async def test_one_held_as_storage_halts_expires_only_once_the_halt_lifts() -> None:
    clock = Clock()
    agents, launcher = agents_for(roles=LIMITED)
    events: asyncio.Queue[Event] = asyncio.Queue()
    hooks = SeatHooks(nothing, lambda: None, tuple, timer=Timer(clock, clock.sleep))
    seats = ConversationSeats(
        AgentDesk(agents, events.put), Tasks(TaskBoard(), events.put), events, hooks
    )
    launcher.planner_turns = [consulting([], asyncio.Event()), chats, chats, chats]
    seats.viewers(1)  # Kept from expiring until the frontend leaves, as the session halts.
    await seats.planner.submit("Plan.", Settings())
    await eventually(lambda: seats.approvals != ())
    await seats.stop_turns(final=False)
    seats.viewers(0)
    for _ in range(10):
        await asyncio.sleep(0)
    halted = drained(events)
    seats.restart_expiry()
    told = "The experimenter's consultation 'Check Y' was interrupted: it expired waiting for"
    await eventually(lambda: any(told in launch.prompt for launch in launcher.launches))
    taken = drained(events)
    await seats.stop_turns(final=True)
    await seats.close()

    assert not any(isinstance(e, ApprovalDecided) for e in halted)
    assert ApprovalDecided(consultation=2, decision="deny") in taken
    assert clock.now == HOUR


def asking(heard: list[Reply], consultant: str, *messages: str) -> Script:
    """A turn consulting ``consultant`` with each of ``messages``, noting what it hears in
    ``heard``."""

    async def turn(call: Call) -> TurnEndReason:
        for message in messages:
            heard.append(await call("consult", role=consultant, message=message))
        return "completed"

    return turn


MUTUAL = {
    **LIMITED,
    EXPERIMENTER: EXPERIMENTER_ROLE.model_copy(
        update={"consults": (PLANNER,), "consult_limit_per_hour": 1}
    ),
}
"""A planner and an experimenter each consulting the other, once an hour."""


def of_kind(events: list[Event], kind: type[ApprovalDecided]) -> list[ApprovalDecided]:
    """The ``events`` of type ``kind``."""
    return [e for e in events if isinstance(e, kind)]


def by(asker: str, asked: list[ApprovalRequested]) -> list[int]:
    """The consultations in ``asked`` from the role ``asker``."""
    return [request.consultation for request in asked if request.asker == asker]


async def holding_from_both() -> tuple[WorkspaceHost, list[ApprovalRequested]]:
    """A session holding two consultations from the planner and one from the experimenter,
    and what the user was asked about them."""
    heard: list[Reply] = []
    session, _, launcher = session_for(roles=MUTUAL)
    launcher.planner_turns = [
        asking(heard, EXPERIMENTER, "Check X", "Check Y", "Check Z"),
        asking(heard, PLANNER, "Ask A", "Ask B"),
        *[chats] * 8,
    ]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    asked: list[ApprovalRequested] = []

    def third(event: Event) -> bool:
        if isinstance(event, ApprovalRequested):
            asked.append(event)
        return len(asked) == 3

    await events_until(session, third)
    return session, asked


async def test_allowing_for_the_hour_starts_the_askers_other_held_consultations() -> None:
    session, asked = await holding_from_both()
    planners = by(PLANNER, asked)
    await send(session, DecideApproval(consultation=planners[0], decision="allow_hour"))
    events = await events_until(session, answering(planners[1]))
    snapshot = await snapshot_of(session)
    await session.close()

    decided = of_kind(events, ApprovalDecided)
    assert decided == [ApprovalDecided(consultation=n, decision="allow_hour") for n in planners]
    assert by(EXPERIMENTER, list(snapshot.approvals)) == by(EXPERIMENTER, asked)
    assert len(snapshot.approvals) == 1


async def test_allowing_once_leaves_the_askers_other_held_consultations_held() -> None:
    session, asked = await holding_from_both()
    planners = by(PLANNER, asked)
    await send(session, DecideApproval(consultation=planners[0], decision="allow"))
    await events_until(session, of_type(ApprovalDecided))
    snapshot = await snapshot_of(session)
    await session.close()

    assert by(PLANNER, list(snapshot.approvals)) == planners[1:]


def holding_host(store: FakeStore) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session saved in ``store``, as ``session-1`` and so on, whose planner may consult the
    experimenter once an hour."""
    agents, launcher = agents_for(roles=LIMITED)
    ids = iter(f"session-{n}" for n in range(1, 100))
    host = WorkspaceHost(agents=agents, store=store, clock=Ticks(), new_id=lambda: next(ids))
    return host, launcher


def second_turn_ends() -> Callable[[Event], bool]:
    """Stops at the planner's second TurnFinished."""
    ended: list[Event] = []

    def done(event: Event) -> bool:
        if isinstance(event, TurnFinished) and event.agent_id == "agent-1":
            ended.append(event)
        return len(ended) == 2

    return done


async def holding_in(store: FakeStore) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session saved in ``store`` whose planner's consultation 2 is held, its turns over."""
    session, launcher = holding_host(store)
    launcher.planner_turns = [consulting([], asyncio.Event()), chats, chats, chats]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, second_turn_ends())
    return session, launcher


def last_of(store: FakeStore, session_id: str) -> SessionRecord:
    """The record of ``session_id`` last saved in ``store``."""
    return [record for record in store.saved if record.id == session_id][-1]


SWITCHED = "the session was switched"


async def test_switching_away_settles_the_one_held_as_denied() -> None:
    store = FakeStore()
    session, _ = await holding_in(store)
    events = await opened(session, NewSession())
    snapshot = await snapshot_of(session)
    await session.close()

    assert ApprovalDecided(consultation=2, decision="deny") in events
    assert snapshot.approvals == ()
    assert [(c.number, c.why) for c in last_of(store, "session-1").consultations] == [(2, SWITCHED)]


async def test_one_held_as_storage_halts_stays_held_to_decide_once_resumed() -> None:
    store = FakeStore()
    session, _ = await holding_in(store)
    halt = halt_of(session)
    halt.fail(FailedWrite.EVENT_LOG, StoreFailed("disk full"))
    await eventually(lambda: not halt._halting)  # The turns stopped.
    await resume_storage(session)
    snapshot = await snapshot_of(session)
    pending = [c.number for c in store.last.consultations]
    await send(session, DecideApproval(consultation=2, decision="allow"))
    answer = sole(await events_until(session, of_type(ConsultationAnswered)), ConsultationAnswered)
    await session.close()

    assert snapshot.approvals == (requested(2, "Check Y"),)
    assert pending == [2]
    assert (answer.consultation, answer.interrupted) == (2, None)


async def reopened(why: str, launcher: ScriptedLauncher) -> None:
    """Checks the planner is told its consultation 2 was interrupted for ``why``."""
    told = f"The experimenter's consultation 'Check Y' was interrupted: {why}."
    await eventually(lambda: any(told in launch.prompt for launch in launcher.launches))


async def test_reopening_a_session_left_holding_one_says_it_was_switched() -> None:
    store = FakeStore()
    session, launcher = await holding_in(store)
    await opened(session, NewSession())
    await send(session, ActivateSession(session="session-1"))
    answer = sole(await events_until(session, answering(2)), ConsultationAnswered)
    await reopened(SWITCHED, launcher)
    await session.close()

    assert answer.interrupted == SWITCHED


async def test_one_held_as_orbit_stops_is_answered_as_the_session_reopens() -> None:
    store = FakeStore()
    session, _ = await holding_in(store)
    await session.close()
    store = store_with(saved("talks", consultations=last_of(store, "session-1").consultations))
    session, launcher = holding_host(store)
    launcher.planner_turns = [chats]
    await send(session, ActivateSession(session="talks"))
    answer = (await events_until(session, answering(2)))[-1]
    assert isinstance(answer, ConsultationAnswered)
    await reopened("orbit restarted", launcher)
    await session.close()

    assert answer.interrupted == "orbit restarted"


async def test_a_switch_that_fails_asks_about_the_one_held_again() -> None:
    store = FakeStore()
    store.saved.append(saved("other"))
    session, _ = await holding_in(store)
    store.elsewhere.add("other")
    events = await opened(session, ActivateSession(session="other"))
    snapshot = await snapshot_of(session)
    await session.close()

    deny = events.index(ApprovalDecided(consultation=2, decision="deny"))
    assert requested(2, "Check Y") in events[deny:]
    assert snapshot.approvals == (requested(2, "Check Y"),)


async def test_a_decision_after_leaving_is_refused_and_leaves_it_held() -> None:
    store = FakeStore()
    session, _ = await holding_in(store)
    session._seats.leave()
    await send(session, DecideApproval(consultation=2, decision="allow"))
    events = await events_until(session, of_type(CommandRefused))
    snapshot = await snapshot_of(session)
    await session.close()

    assert events[-1] == refused(UNKNOWN, "No consultation 2 is waiting for approval.")
    assert [e.decision for e in events if isinstance(e, ApprovalDecided)] == ["deny"]
    assert snapshot.approvals == (requested(2, "Check Y"),)
    assert [c.number for c in store.last.consultations] == [2]


async def test_a_decision_after_leaving_then_staying_starts_it() -> None:
    store = FakeStore()
    session, _ = await holding_in(store)
    session._seats.leave()
    session._seats.stay()
    await send(session, DecideApproval(consultation=2, decision="allow"))
    answer = sole(await events_until(session, of_type(ConsultationAnswered)), ConsultationAnswered)
    await session.close()

    assert (answer.consultation, answer.interrupted) == (2, None)


async def test_a_decision_on_one_held_by_the_session_left_is_refused() -> None:
    store = FakeStore()
    session, _ = await holding_in(store)
    await opened(session, NewSession())
    await send(session, DecideApproval(consultation=2, decision="allow"))
    events = await events_until(session, of_type(CommandRefused))
    await session.close()

    assert events[-1] == refused(UNKNOWN, "No consultation 2 is waiting for approval.")
    assert not any(isinstance(e, ApprovalDecided) for e in events)
