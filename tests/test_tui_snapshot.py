"""The TUI replacing what it shows with a snapshot of the open session."""

from orbit.protocol import (
    PLANNER,
    PLANNER_LANE,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    BusyChanged,
    ChecksProposed,
    CommandAccepted,
    CommandRefused,
    ConversationReset,
    DecideProposal,
    Event,
    EventEnvelope,
    MessageSent,
    Notice,
    ProposalDecided,
    ProposedStep,
    RunningAgent,
    SessionSummary,
    TaskStarted,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from orbit.tui import OrbitApp
from orbit.tui.agents import AgentsPane
from orbit.tui.panels import VerificationPanel
from orbit.tui.proposals import ProposalScreen
from orbit.tui.tasks import TaskList
from orbit.tui.transcript import Transcript
from orbit.tui.view.agents import AgentRow
from orbit.tui.widgets import ChatPrompt
from tests.tui_fakes import (
    CODER,
    PINNED,
    SNAPSHOT,
    STRIP,
    TASKS,
    FakeSession,
    entries,
    feed_events,
    opened,
    tab_strip,
)

PROPOSAL = ChecksProposed(
    agent_id="agent-3", task_id="1", steps=(ProposedStep(name="Lint", command=("ruff",)),)
)


async def test_a_snapshot_shows_its_tasks_busy_planner_agents_and_steps() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT)
        task_list = app.query_one(TaskList)
        assert task_list.tasks == TASKS
        assert task_list.border_subtitle == "session: 2k tok · $2.00"
        prompt = app.query_one(ChatPrompt)
        assert prompt.border_title == "Prompt · planner · busy"
        assert prompt.border_subtitle == "planner's model: opus · new tasks' tools: restricted"
        pane = app.query_one(AgentsPane)
        assert pane.rows == (AgentRow("agent-3", "coder", "1", (("r1", "propose_checks"),)),)
        assert pane.border_subtitle == "review: paused"
        panel = app.query_one(VerificationPanel)
        assert (panel.steps, panel.title) == (SNAPSHOT.steps, "Startup check")
        assert str(app.query_one(Transcript).border_title) == "Conversation — auth rework"


async def test_a_read_only_snapshot_marks_the_prompt_until_one_that_isnt() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"read_only": True}))
        prompt = app.query_one(ChatPrompt)
        assert prompt.border_title == "Prompt · planner · busy · read-only"
        await feed_events(session, pilot, BusyChanged(busy=False, lane=PLANNER_LANE))
        assert prompt.border_title == "Prompt · planner · read-only"
        await feed_events(session, pilot, SNAPSHOT)
        assert prompt.border_title == "Prompt · planner · busy"


async def test_a_snapshot_replaces_the_agents_and_the_transcript() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        started = AgentStarted(agent_id="agent-1", role="planner", task_id="plan")
        await feed_events(session, pilot, started, Notice(message="Workspace: /work"))
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"agents": ()}))
        assert app.query_one(AgentsPane).rows == ()
        assert entries(app) == []
        assert app.query_one(Transcript).entries(PLANNER) == ()


def logged(*events: Event) -> tuple[EventEnvelope, ...]:
    """``events`` as a snapshot's transcript, numbered from 1."""
    return tuple(
        EventEnvelope(seq=seq, session="s1", payload=event)
        for seq, event in enumerate(events, start=1)
    )


PLANNER_TURN = (
    TurnStarted(turn_id="p1", agent_id=PLANNER),
    AssistantMessage(turn_id="p1", agent_id=PLANNER, text="Two tasks, then."),
    TurnFinished(turn_id="p1", agent_id=PLANNER, reason="completed"),
)
CODER_CALL = (
    AgentStarted(agent_id="agent-3", role="coder", task_id="1"),
    ToolCallStarted(turn_id="c1", agent_id="agent-3", call_id="x", name="Read", arguments={}),
    ToolCallFinished(turn_id="c1", agent_id="agent-3", call_id="x", output="text"),
)


def texts(app: OrbitApp, key: str | None) -> list[str]:
    """The texts of the entries under the tab keyed ``key``."""
    return [entry.text for entry in app.query_one(Transcript).entries(key)]


async def test_a_snapshots_transcript_shows_its_turns_and_tool_calls_in_their_tabs() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    tail = SNAPSHOT.model_copy(update={"transcript": logged(*PLANNER_TURN, *CODER_CALL)})
    async with app.run_test() as pilot:
        await feed_events(session, pilot, tail)
        assert "Two tasks, then." in texts(app, PLANNER)
        [call] = [entry for entry in app.query_one(Transcript).entries("coder") if entry.call]
        assert call.call is not None and call.call.status == "passed"
        assert texts(app, None)[0] == "Two tasks, then."


async def test_a_snapshots_transcript_shows_the_users_prompt_and_decision() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    prompt = MessageSent(command_id="m", agent=PLANNER, text="add a login page")
    decision = ProposalDecided(command_id="d", agent_id="agent-3", task_id="1", action="end")
    transcript = logged(prompt, *PLANNER_TURN, CODER_CALL[0], decision)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"transcript": transcript}))
        assert texts(app, PLANNER)[:2] == ["add a login page", "Two tasks, then."]
        assert texts(app, None)[0] == "add a login page"
        assert texts(app, None)[-1] == "orbit → agent-3: task ended"
        assert texts(app, "coder")[-1] == "orbit → agent-3: task ended"


async def test_a_later_snapshot_of_another_session_replaces_the_transcript() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    tail = SNAPSHOT.model_copy(update={"transcript": logged(*PLANNER_TURN, *CODER_CALL)})
    other = opened(SessionSummary(id="s2", name="docs"))
    note = Notice(message="Notes on docs.")
    async with app.run_test() as pilot:
        await feed_events(session, pilot, tail)
        await feed_events(session, pilot, other.model_copy(update={"transcript": logged(note)}))
        assert texts(app, None) == ["📌 Notes on docs."]
        assert texts(app, PLANNER) == []
        assert texts(app, "coder") == []
        assert str(app.query_one(Transcript).border_title) == "Conversation — docs"


async def test_a_snapshot_closes_the_agent_tabs_its_transcript_doesnt_open() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    checker = AgentStarted(agent_id="agent-4", role="checker", task_id="1")
    running = RunningAgent(agent_id="agent-4", role="checker", task_id="1")
    empty = opened(SessionSummary(id="s1", name="auth"))
    async with app.run_test() as pilot:
        await feed_events(session, pilot, checker)
        assert tab_strip(app) == [*STRIP, "checker"]
        await feed_events(session, pilot, empty)
        assert tab_strip(app) == STRIP
        reopened = empty.model_copy(update={"transcript": logged(checker), "agents": (running,)})
        await feed_events(session, pilot, reopened)
        assert tab_strip(app) == [*STRIP, "checker"]


async def test_a_snapshots_agents_started_but_not_running_get_no_tab_or_marker() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    checker = AgentStarted(agent_id="agent-4", role="checker", task_id="1")
    crashed = opened(SessionSummary(id="s1", name="auth")).model_copy(
        update={"transcript": logged(*CODER_CALL, checker)}
    )
    async with app.run_test() as pilot:
        await feed_events(session, pilot, crashed)
        assert tab_strip(app) == STRIP
        assert "Read" in "".join(texts(app, "coder"))


async def test_a_running_agent_started_before_the_transcript_gets_its_roles_tab() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    checker = RunningAgent(agent_id="agent-4", role="checker", task_id="2")
    cut = SNAPSHOT.model_copy(
        update={"agents": (CODER, checker), "transcript": logged(*CODER_CALL[1:])}
    )
    async with app.run_test() as pilot:
        await feed_events(session, pilot, cut)
        assert tab_strip(app) == ["All", "● planner", "● coder", *PINNED[2:], "checker"]
        [call] = [entry for entry in app.query_one(Transcript).entries("coder") if entry.call]
        assert call.call is not None and call.call.status == "passed"
        assert texts(app, "agent-3") == []


REVIEWER_TURN = (
    AgentStarted(agent_id="agent-5", role="reviewer", task_id="2"),
    AssistantMessage(turn_id="r1", agent_id="agent-5", text="Looks good."),
    TurnFinished(turn_id="r1", agent_id="agent-5", reason="completed"),
)


async def test_a_snapshots_transcript_marks_no_tab_unread() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    transcript = logged(TaskStarted(task_id="1"), *PLANNER_TURN, *CODER_CALL, *REVIEWER_TURN)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"transcript": transcript}))
        assert tab_strip(app) == ["All", "● planner", "● coder", *PINNED[2:]]
        assert "Looks good." in texts(app, "reviewer")


async def test_a_live_event_after_a_snapshot_marks_its_tab_unread() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    said = AssistantMessage(turn_id="c2", agent_id="agent-3", text="Done.")
    async with app.run_test() as pilot:
        await feed_events(
            session, pilot, SNAPSHOT.model_copy(update={"transcript": logged(*CODER_CALL)})
        )
        await feed_events(session, pilot, said, TaskStarted(task_id="1"))
        assert tab_strip(app) == ["All", "● planner", "● coder *", *PINNED[2:]]


CRASHED_CALL = CODER_CALL[:2]
"""A coder's call whose orbit crashed before it finished: no result, and no exit."""


def call_statuses(app: OrbitApp, key: str | None) -> list[str]:
    """The statuses of the calls and requests under the tab keyed ``key``."""
    return [e.call.status for e in app.query_one(Transcript).entries(key) if e.call is not None]


async def test_a_replayed_call_of_an_agent_not_running_shows_no_result() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    crashed = SNAPSHOT.model_copy(update={"agents": (), "transcript": logged(*CRASHED_CALL)})
    async with app.run_test() as pilot:
        await feed_events(session, pilot, crashed)
        assert call_statuses(app, "coder") == ["no_result"]
        assert call_statuses(app, None) == ["no_result"]
        assert not app.query_one(Transcript).ticking


async def test_a_replayed_call_of_a_running_agent_stays_running() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    running = SNAPSHOT.model_copy(update={"transcript": logged(*CRASHED_CALL)})
    async with app.run_test() as pilot:
        await feed_events(session, pilot, running)
        assert call_statuses(app, "coder") == ["running"]
        assert app.query_one(Transcript).ticking


async def test_a_replayed_request_of_an_agent_not_running_shows_no_result() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    request = AgentRequest(agent_id="agent-3", request_id="r1", tool="get_task", arguments={})
    crashed = SNAPSHOT.model_copy(
        update={"agents": (), "transcript": logged(CODER_CALL[0], request)}
    )
    async with app.run_test() as pilot:
        await feed_events(session, pilot, crashed)
        assert call_statuses(app, "coder") == ["no_result"]
        assert not app.query_one(Transcript).ticking


async def test_a_replayed_call_of_the_planner_listed_running_stays_running() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    planner = RunningAgent(agent_id="agent-1", role=PLANNER, task_id="plan")
    started = AgentStarted(agent_id="agent-1", role=PLANNER, task_id="plan")
    call = ToolCallStarted(turn_id="p1", agent_id="agent-1", call_id="y", name="Read", arguments={})
    busy = SNAPSHOT.model_copy(
        update={"agents": (planner, CODER), "transcript": logged(started, call, *CRASHED_CALL)}
    )
    async with app.run_test() as pilot:
        await feed_events(session, pilot, busy)
        assert call_statuses(app, PLANNER) == ["running"]
        assert call_statuses(app, "coder") == ["running"]


RESTARTED_CALL = (
    AgentStarted(agent_id="agent-3", role="coder", task_id="2"),
    ToolCallStarted(turn_id="c2", agent_id="agent-3", call_id="y", name="Read", arguments={}),
)
"""A coder's call in a run orbit started, after a restart, under the crashed run's id."""


async def test_a_replayed_call_of_an_earlier_run_under_a_running_agents_id_shows_no_result() -> (
    None
):
    session = FakeSession()
    app = OrbitApp(session)
    restarted = SNAPSHOT.model_copy(update={"transcript": logged(*CRASHED_CALL, *RESTARTED_CALL)})
    async with app.run_test() as pilot:
        await feed_events(session, pilot, restarted)
        assert call_statuses(app, "coder") == ["no_result", "running"]
        assert call_statuses(app, None) == ["no_result", "running"]


async def test_a_replayed_request_of_an_earlier_run_under_a_running_agents_id_shows_no_result() -> (
    None
):
    session = FakeSession()
    app = OrbitApp(session)
    request = AgentRequest(agent_id="agent-3", request_id="r0", tool="get_task", arguments={})
    events = logged(CODER_CALL[0], request, *RESTARTED_CALL)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"transcript": events}))
        assert call_statuses(app, "coder") == ["no_result", "running"]
        assert call_statuses(app, None) == ["no_result", "running"]


async def test_an_agent_starting_under_an_id_ends_the_earlier_runs_calls_live() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"agents": ()}))
        await feed_events(session, pilot, *CRASHED_CALL)
        assert app.query_one(Transcript).ticking
        await feed_events(session, pilot, RESTARTED_CALL[0])
        assert call_statuses(app, "coder") == ["no_result"]
        assert not app.query_one(Transcript).ticking


async def test_a_snapshots_transcript_shows_a_reset_and_a_refusal_as_they_were_shown() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    refused = CommandRefused(command_id="c", code="busy", message="Task 1 is running.")
    tail = logged(ConversationReset(agent=PLANNER), refused, CommandAccepted(command_id="d"))
    async with app.run_test() as pilot:
        await feed_events(session, pilot, ConversationReset(agent=PLANNER), refused)
        live = entries(app)
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"transcript": tail}))
        assert entries(app) == live


async def test_a_snapshot_with_a_waiting_proposal_shows_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"proposals": (PROPOSAL,)}))
        screen = app.screen
        assert isinstance(screen, ProposalScreen)
        assert screen.proposal == PROPOSAL


async def test_a_snapshot_still_waiting_on_the_shown_proposal_keeps_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    waiting = SNAPSHOT.model_copy(update={"proposals": (PROPOSAL,)})
    async with app.run_test() as pilot:
        await feed_events(session, pilot, PROPOSAL)
        shown = app.screen
        await feed_events(session, pilot, waiting)
        assert app.screen is shown


async def test_a_snapshot_without_the_shown_proposal_closes_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, PROPOSAL)
        assert isinstance(app.screen, ProposalScreen)
        await feed_events(session, pilot, SNAPSHOT)
        assert not isinstance(app.screen, ProposalScreen)
        assert session.commands == []


async def test_a_snapshot_with_another_proposal_shows_that_one_in_place_of_the_shown() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    other = PROPOSAL.model_copy(update={"task_id": "2"})
    async with app.run_test() as pilot:
        await feed_events(session, pilot, PROPOSAL)
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"proposals": (other,)}))
        screen = app.screen
        assert isinstance(screen, ProposalScreen)
        assert screen.proposal == other
        await pilot.press("a")
        await pilot.pause()
        assert not isinstance(app.screen, ProposalScreen)


def proposal_screens(app: OrbitApp) -> list[ProposalScreen]:
    """The proposal screens on ``app``'s stack."""
    return [screen for screen in app.screen_stack if isinstance(screen, ProposalScreen)]


async def test_the_proposal_event_after_a_snapshot_showing_it_adds_no_second_screen() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"proposals": (PROPOSAL,)}))
        await feed_events(session, pilot, PROPOSAL)
        assert len(proposal_screens(app)) == 1
        await pilot.press("a")
        await pilot.pause()
        assert session.commands == [DecideProposal(task_id="1", action="accept")]
        assert proposal_screens(app) == []


async def test_another_proposal_event_while_one_is_shown_waits_for_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    other = PROPOSAL.model_copy(update={"task_id": "2"})
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"proposals": (PROPOSAL,)}))
        await feed_events(session, pilot, other)
        assert [screen.proposal for screen in proposal_screens(app)] == [PROPOSAL]
        await pilot.press("a")
        assert [screen.proposal for screen in proposal_screens(app)] == [other]
