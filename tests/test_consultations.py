"""Consultations: a conversational role asks another through ``consult``, which answers in its
own line, in order with the user's messages to it, and the reply reaches the asker as a message:
at once if it's idle, after its turn and any message of the user's waiting otherwise, several
together. A consulted turn runs in an ``orbit.consultation`` span linked to the asking turn."""

import asyncio
from collections.abc import Callable

from orbit.agent import WorkspaceHost
from orbit.agent.application.consultations import title_of
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.planner import PlannerChat
from orbit.agent.application.planner_trace import CONSULTATION_SPAN, TURN_SPAN
from orbit.agent.application.ports import Reply, StoreFailed
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.consultations import failed
from orbit.agent.domain.roles import Role
from orbit.agent.domain.runtimes import CLAUDE, Failure
from orbit.agent.domain.sessions import SavedConsultation
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.protocol import (
    PLANNER,
    ActivateSession,
    CancelTurn,
    ConsultationAnswered,
    ConsultationAsked,
    Event,
    NewSession,
    SendMessage,
    Snapshot,
    TurnEndReason,
    TurnFinished,
    Usage,
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
    idle,
    resume_storage,
    send,
    session_for,
    snapshot_of,
    sole,
    switcher_of,
)
from tests.session_builders import opened, saved, store_with
from tests.telemetry_helpers import Traced, linked_to
from tests.waiting import eventually

EXPERIMENTER = "experimenter"
EXPERIMENTER_ROLE = ROLES[PLANNER].model_copy(
    update={"instructions": "You experiment.", "lane": EXPERIMENTER}
)
CONSULTING_PLANNER = ROLES[PLANNER].model_copy(update={"consults": (EXPERIMENTER,)})

CONSULTING = {**ROLES, PLANNER: CONSULTING_PLANNER, EXPERIMENTER: EXPERIMENTER_ROLE}
"""The built-in roles, with a planner consulting an experimenter in its own lane."""

ASKED = "Asked the experimenter (consultation {}); its reply will arrive as a message."


def consults(
    *messages: str, heard: list[Reply], gate: asyncio.Event | None = None, role: str = EXPERIMENTER
) -> Script:
    """A turn consulting ``role`` with each of ``messages``, noting what it hears in ``heard``,
    then waiting for ``gate``, if given."""

    async def turn(call: Call) -> TurnEndReason:
        for message in messages:
            heard.append(await call("consult", role=role, message=message))
        if gate is not None:
            await gate.wait()
        return "completed"

    return turn


def gated(gate: asyncio.Event) -> Script:
    async def turn(call: Call) -> TurnEndReason:
        await gate.wait()
        return "completed"

    return turn


async def fails(call: Call) -> TurnEndReason:
    raise RuntimeError("The experiment broke.")


def finished(agent_id: str, count: int) -> Callable[[Event], bool]:
    """Stops at agent ``agent_id``'s ``count``-th TurnFinished."""
    seen: list[Event] = []

    def done(event: Event) -> bool:
        if isinstance(event, TurnFinished) and event.agent_id == agent_id:
            seen.append(event)
        return len(seen) == count

    return done


def answered(count: int) -> Callable[[Event], bool]:
    """Stops at the ``count``-th ConsultationAnswered."""
    seen: list[Event] = []

    def done(event: Event) -> bool:
        if isinstance(event, ConsultationAnswered):
            seen.append(event)
        return len(seen) == count

    return done


def asked(position: int, number: int = 1, title: str = "Check X") -> ConsultationAsked:
    return ConsultationAsked(
        consultation=number,
        asker=PLANNER,
        consultant=EXPERIMENTER,
        title=title,
        position=position,
    )


def reply(number: int, title: str, text: str) -> str:
    return f"Experimenter's reply to '{title}' (consultation {number}):\n{text}"


def prompts(launcher: ScriptedLauncher) -> list[tuple[str, str]]:
    """Each launch's role and the prompt's last paragraph."""
    return [(launch.agent.role, launch.prompt) for launch in launcher.launches]


async def test_a_consultation_is_answered_and_its_reply_starts_the_idle_askers_turn() -> None:
    heard: list[Reply] = []
    session, _, launcher = session_for(roles=CONSULTING)
    launcher.planner_turns = [consults("Check X\nDetails", heard=heard), chats, chats]
    launcher.planner_replies = ["Asking.", "X is 3", "Thanks."]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    events = await events_until(session, finished("agent-1", 2))
    await session.close()

    assert heard == [Reply(ASKED.format(1))]
    assert asked(1) in events
    assert sole(events, ConsultationAnswered) == ConsultationAnswered(
        consultation=1, asker=PLANNER, consultant=EXPERIMENTER, title="Check X", reply="X is 3"
    )
    (_, first), (consulted, question), (asker, delivered) = prompts(launcher)
    assert first.endswith("Plan.")
    assert (consulted, asker) == (EXPERIMENTER, PLANNER)
    assert question.endswith("Check X\nDetails")
    assert delivered.endswith(reply(1, "Check X", "X is 3"))


async def test_replies_wait_for_the_askers_turn_and_a_users_message_then_come_together() -> None:
    heard: list[Reply] = []
    gate = asyncio.Event()
    session, _, launcher = session_for(roles=CONSULTING)
    planning = consults("Check X", "Check Y", heard=heard, gate=gate)
    launcher.planner_turns = [planning, chats, chats, chats, chats]
    launcher.planner_replies = ["Asking.", "X is 3", "Y is 4", "Sure.", "Thanks."]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    events = await events_until(session, answered(2))
    assert len(launcher.launches) == 3  # The planner's turn still runs.
    await send(session, SendMessage(agent=PLANNER, text="Also Z."))
    gate.set()
    events += await events_until(session, finished("agent-1", 3))
    await session.close()

    assert heard == [Reply(ASKED.format(1)), Reply(ASKED.format(2))]
    assert [e.position for e in events if isinstance(e, ConsultationAsked)] == [1, 2, 1]
    *_, (_, user), (_, delivered) = prompts(launcher)
    assert user.endswith("Also Z.")
    together = f"{reply(1, 'Check X', 'X is 3')}\n\n{reply(2, 'Check Y', 'Y is 4')}"
    assert delivered.endswith(together)


async def test_a_consultation_waits_second_in_line_while_its_consultant_answers_the_user() -> None:
    heard: list[Reply] = []
    gate = asyncio.Event()
    session, _, launcher = session_for(roles=CONSULTING)
    launcher.planner_turns = [gated(gate), consults("Check X", heard=heard), chats, chats]
    await send(session, SendMessage(agent=EXPERIMENTER, text="Try it."))
    await eventually(lambda: launcher.launches, poll=0)
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    events = await events_until(session, lambda event: isinstance(event, ConsultationAsked))
    assert events[-1] == asked(2)
    gate.set()
    events = await events_until(session, finished("agent-2", 2))  # The planner's.
    await session.close()

    assert asked(1) in events
    roles = [role for role, _ in prompts(launcher)]
    assert roles == [EXPERIMENTER, PLANNER, EXPERIMENTER, PLANNER]
    assert prompts(launcher)[2][1].endswith("Check X")


async def failing(*failures: tuple[str, Failure]) -> tuple[list[Event], str]:
    """The events of a planner's turn consulting the experimenter, whose turn fails with
    "The experiment broke.", up to the planner's turn hearing of it, and that turn's prompt; the
    launcher tells each of ``failures``."""
    heard: list[Reply] = []
    session, _, launcher = session_for(roles=CONSULTING)
    launcher.failures = dict(failures)
    launcher.planner_turns = [consults("Check X", heard=heard), fails, chats]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    events = await events_until(session, finished("agent-1", 2))
    await session.close()
    role, prompt = prompts(launcher)[-1]
    assert role == PLANNER
    return events, prompt


async def test_a_failed_consulted_turn_is_interrupted_with_its_error() -> None:
    events, prompt = await failing()
    assert sole(events, ConsultationAnswered) == interrupted("it failed: The experiment broke.")
    assert prompt.endswith(told("it failed: The experiment broke."))


async def test_a_consulted_turn_stopped_by_the_usage_limit_names_it_and_the_runtime() -> None:
    events, prompt = await failing(("The experiment broke.", Failure(kind="usage_limit")))
    assert prompt.endswith(told("Claude's usage limit was reached"))
    assert sole(events, ConsultationAnswered) == interrupted("Claude's usage limit was reached")


async def refusal(roles: dict[str, Role], *messages: str, role: str = EXPERIMENTER) -> Reply:
    """What the planner hears consulting ``role`` with ``messages`` among ``roles``."""
    heard: list[Reply] = []
    session, _, launcher = session_for(roles=roles)
    launcher.planner_turns = [consults(*messages, heard=heard, role=role)]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, finished("agent-1", 1))
    await session.close()
    [refused] = heard
    return refused


async def test_a_role_without_consults_calling_consult_is_refused() -> None:
    roles = {**CONSULTING, PLANNER: ROLES[PLANNER].model_copy(update={"consults": ()})}
    refused = await refusal(roles, "Check X")
    assert refused == Reply("Your role (planner) can't use consult.", is_error=True)


async def test_consulting_a_role_it_doesnt_consult_is_refused() -> None:
    refused = await refusal(CONSULTING, "Check X", role="architect")
    message = "The planner can't consult 'architect'; it consults experimenter."
    assert refused == Reply(message, is_error=True)


async def test_a_consultation_without_a_message_is_refused() -> None:
    refused = await refusal(CONSULTING, "  \n")
    assert refused == Reply("A consultation needs a message.", is_error=True)


async def test_consulting_a_disabled_role_is_refused() -> None:
    disabled = EXPERIMENTER_ROLE.model_copy(update={"enabled": False})
    refused = await refusal({**CONSULTING, EXPERIMENTER: disabled}, "Check X")
    assert refused == Reply("The experimenter can't be consulted now.", is_error=True)


async def test_consulting_a_disabled_planner_is_refused() -> None:
    heard: list[Reply] = []
    architect = ROLES[PLANNER].model_copy(update={"lane": "design", "consults": (PLANNER,)})
    muted = ROLES[PLANNER].model_copy(update={"enabled": False})
    session, _, launcher = session_for(roles={**ROLES, PLANNER: muted, "architect": architect})
    launcher.planner_turns = [consults("Check X", heard=heard, role=PLANNER)]
    await send(session, SendMessage(agent="architect", text="Design."))
    await events_until(session, finished("agent-1", 1))
    await session.close()
    assert heard == [Reply("The planner can't be consulted now.", is_error=True)]


async def test_a_chat_outside_a_seat_has_no_one_to_consult() -> None:
    async def publish(event: Event) -> None:
        pass

    agents, _ = agents_for(roles=CONSULTING)
    chat = PlannerChat(AgentDesk(agents, publish), Tasks(TaskBoard(), publish), lambda: ())
    answer = await chat.handle("consult", {"role": EXPERIMENTER, "message": "Check X"})
    assert answer == Reply("The planner has no one to consult.", is_error=True)


async def test_the_consulted_turns_span_starts_a_trace_linked_to_the_asking_turn() -> None:
    traced = Traced()
    heard: list[Reply] = []
    session, _, launcher = session_for(roles=CONSULTING, telemetry=traced.telemetry)
    launcher.planner_turns = [consults("Check X", heard=heard), chats, chats]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, finished("agent-1", 2))
    await session.close()

    [consultation] = traced.spans(CONSULTATION_SPAN)
    first, consulted, _ = sorted(traced.spans(TURN_SPAN), key=lambda span: span.start_time or 0)
    assert consultation.parent is None
    assert linked_to(consultation, first)
    assert consulted.parent is not None and consultation.context is not None
    assert consulted.parent.span_id == consultation.context.span_id
    attributes = dict(consultation.attributes or {})
    assert attributes["orbit.consultation.asker"] == PLANNER
    assert attributes["orbit.consultation.consultant"] == EXPERIMENTER
    assert attributes["orbit.consultation.outcome"] == "completed"
    assert isinstance(attributes["orbit.consultation.wait_ms"], int)


def test_a_consultations_title_is_its_messages_first_line() -> None:
    assert title_of("  Check X  \nDetails") == "Check X"


async def test_a_users_message_joins_the_line_behind_a_waiting_consultation() -> None:
    heard: list[Reply] = []
    gate = asyncio.Event()
    session, _, launcher = session_for(roles=CONSULTING)
    launcher.planner_turns = [gated(gate), consults("Check X", heard=heard), chats, chats, chats]
    await send(session, SendMessage(agent=EXPERIMENTER, text="Try it."))
    await eventually(lambda: launcher.launches, poll=0)
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    events = await events_until(session, lambda event: isinstance(event, ConsultationAsked))
    await send(session, SendMessage(agent=EXPERIMENTER, text="And this."))
    gate.set()
    events += await events_until(session, finished("agent-1", 3))  # The experimenter's.
    await session.close()

    assert [e for e in events if isinstance(e, ConsultationAsked)] == [asked(2), asked(1)]
    experimenter = [prompt for role, prompt in prompts(launcher) if role == EXPERIMENTER]
    assert experimenter[1].endswith("Check X")
    assert experimenter[2].endswith("And this.")


def interrupted(why: str, asker: str = PLANNER) -> ConsultationAnswered:
    return ConsultationAnswered(
        consultation=1,
        asker=asker,
        consultant=EXPERIMENTER,
        title="Check X",
        reply="",
        interrupted=why,
    )


def told(why: str) -> str:
    """The message telling the planner its consultation was interrupted for ``why``."""
    return f"The experimenter's consultation 'Check X' was interrupted: {why}."


def test_a_consulted_turn_stopped_by_an_outage_names_the_runtimes_api() -> None:
    outage = Failure(kind="api_unavailable")
    assert failed("API Error: 529", outage, CLAUDE) == "Claude's API wasn't answering"


async def test_cancelling_the_consulted_turn_tells_the_asker_it_was_cancelled() -> None:
    heard: list[Reply] = []
    session, _, launcher = session_for(roles=CONSULTING)
    launcher.planner_turns = [consults("Check X", heard=heard), gated(asyncio.Event()), chats]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await eventually(lambda: len(launcher.launches) == 2, poll=0)
    await send(session, CancelTurn(lane=EXPERIMENTER))
    events = await events_until(session, finished("agent-1", 2))
    await session.close()

    assert sole(events, ConsultationAnswered) == interrupted("it was cancelled")
    role, prompt = prompts(launcher)[-1]
    assert (role, prompt.endswith(told("it was cancelled"))) == (PLANNER, True)


def consulting_host(store: FakeStore) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session saved in ``store``, as ``session-1`` and so on, whose planner consults the
    experimenter."""
    agents, launcher = agents_for(roles=CONSULTING)
    ids = iter(f"session-{n}" for n in range(1, 100))
    host = WorkspaceHost(agents=agents, store=store, clock=Ticks(), new_id=lambda: next(ids))
    return host, launcher


PENDING = SavedConsultation(number=1, asker=PLANNER, consultant=EXPERIMENTER, title="Check X")


async def asking(store: FakeStore, gate: asyncio.Event) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session saved in ``store`` whose planner consulted the experimenter, whose turn
    waits for ``gate``."""
    heard: list[Reply] = []
    session, launcher = consulting_host(store)
    launcher.planner_turns = [consults("Check X", heard=heard), gated(gate), chats]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await eventually(lambda: len(launcher.launches) == 2, poll=0)
    return session, launcher


async def test_stopping_orbit_saves_the_consultation_in_progress_unanswered() -> None:
    store = FakeStore()
    session, _ = await asking(store, asyncio.Event())
    await session.close()
    assert store.last.consultations == (PENDING,)
    assert not any(isinstance(e.payload, ConsultationAnswered) for e in store.logged)


async def test_a_session_saved_with_a_consultation_tells_the_asker_orbit_restarted() -> None:
    store = store_with(saved("talks", consultations=(PENDING,)))
    session, launcher = consulting_host(store)
    launcher.planner_turns = [chats]
    events = await opened(session, ActivateSession(session="talks"))
    events += await events_until(session, finished("agent-1", 1))
    await session.close()

    assert sole(events, ConsultationAnswered) == interrupted("orbit restarted")
    [(role, prompt)] = prompts(launcher)
    assert (role, prompt.endswith(told("orbit restarted"))) == (PLANNER, True)
    assert store.last.consultations == ()


async def test_a_switch_saves_the_consultations_left_pending_as_switched_away_from() -> None:
    store = FakeStore()
    session, _ = await asking(store, asyncio.Event())
    halt_of(session).fail(FailedWrite.EVENT_LOG, StoreFailed("disk full"))  # Stops the turns.
    await events_until(session, idle(EXPERIMENTER))
    await resume_storage(session)
    await opened(session, NewSession())
    await session.close()

    *_, left = [record for record in store.saved if record.id == "session-1"]
    assert left.consultations == (PENDING.model_copy(update={"why": "the session was switched"}),)


async def test_a_session_left_with_a_consultation_tells_the_asker_it_was_switched() -> None:
    switched = PENDING.model_copy(update={"why": "the session was switched"})
    store = store_with(saved("talks", consultations=(switched,)))
    session, launcher = consulting_host(store)
    launcher.planner_turns = [chats]
    events = await opened(session, ActivateSession(session="talks"))
    events += await events_until(session, finished("agent-1", 1))
    await session.close()

    assert sole(events, ConsultationAnswered) == interrupted("the session was switched")
    assert prompts(launcher)[0][1].endswith(told("the session was switched"))


async def test_a_consultation_saved_for_a_role_without_a_seat_is_still_answered() -> None:
    ghost = PENDING.model_copy(update={"asker": "ghost"})
    store = store_with(saved("talks", consultations=(ghost,)))
    session, launcher = consulting_host(store)
    events = await opened(session, ActivateSession(session="talks"))
    await session.close()

    assert sole(events, ConsultationAnswered) == interrupted("orbit restarted", asker="ghost")
    assert launcher.launches == []


PLANNER_SPENT = Usage(cost_usd=0.4)
EXPERIMENTER_SPENT = Usage(cost_usd=1.1)
CAUSED = {PLANNER: {EXPERIMENTER: EXPERIMENTER_SPENT}}


async def consulted_spend(*errors: str, store: FakeStore | None = None) -> Snapshot:
    """The snapshot, once the planner hears the consultation answered, of a session, saved in
    ``store`` if given, whose planner's turn costing $0.40 consulted the experimenter, whose turn
    costs $1.10; the turns end in ``errors``, in the order they end, and the session is closed."""
    heard: list[Reply] = []
    gate = asyncio.Event()
    session, _, launcher = session_for(roles=CONSULTING, store=store)
    launcher.planner_turns = [consults("Check X", heard=heard, gate=gate), chats, chats]
    # The experimenter's turn ends while the planner's waits for the gate.
    launcher.planner_usages = [EXPERIMENTER_SPENT, PLANNER_SPENT]
    launcher.planner_errors = list(errors)
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, finished("agent-2", 1))
    gate.set()
    await events_until(session, finished("agent-1", 2))
    assert switcher_of(session).spent == EXPERIMENTER_SPENT + PLANNER_SPENT
    snapshot = await snapshot_of(session)
    await session.close()
    return snapshot


async def test_a_consulted_turn_counts_for_its_consultant_and_as_caused_by_its_asker() -> None:
    snapshot = await consulted_spend()
    by_role = {PLANNER: PLANNER_SPENT, EXPERIMENTER: EXPERIMENTER_SPENT}
    assert (snapshot.role_spent, snapshot.caused_spent) == (by_role, CAUSED)


async def test_an_interrupted_consulted_turn_still_counts_for_both() -> None:
    snapshot = await consulted_spend("The experiment broke.")
    assert snapshot.role_spent[EXPERIMENTER] == EXPERIMENTER_SPENT
    assert snapshot.caused_spent == CAUSED


async def test_the_spend_by_role_is_saved_and_read_back_as_the_session_opens() -> None:
    store = FakeStore()
    await consulted_spend(store=store)
    by_role = {PLANNER: PLANNER_SPENT, EXPERIMENTER: EXPERIMENTER_SPENT}
    assert (store.last.role_spent, store.last.caused_spent) == (by_role, CAUSED)
    session, _ = consulting_host(store_with(store.last.model_copy(update={"id": "talks"})))
    await opened(session, ActivateSession(session="talks"))
    snapshot = await snapshot_of(session)
    await opened(session, NewSession())
    cleared = await snapshot_of(session)
    await session.close()

    assert (snapshot.role_spent, snapshot.caused_spent) == (by_role, CAUSED)
    assert (cleared.role_spent, cleared.caused_spent) == ({}, {})


async def test_an_older_session_opens_having_spent_nothing_by_role() -> None:
    session, _ = consulting_host(store_with(saved("talks")))
    await opened(session, ActivateSession(session="talks"))
    snapshot = await snapshot_of(session)
    await session.close()
    assert (snapshot.role_spent, snapshot.caused_spent) == ({}, {})


async def test_messaging_the_consultant_directly_counts_for_it_alone() -> None:
    session, _, launcher = session_for(roles=CONSULTING)
    launcher.planner_turns = [chats]
    launcher.planner_usages = [EXPERIMENTER_SPENT]
    await send(session, SendMessage(agent=EXPERIMENTER, text="Try it."))
    await events_until(session, finished("agent-1", 1))
    snapshot = await snapshot_of(session)
    await session.close()

    assert (snapshot.role_spent, snapshot.caused_spent) == ({EXPERIMENTER: EXPERIMENTER_SPENT}, {})
