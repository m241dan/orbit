"""The frontend replacing what it shows with a snapshot of the open session, without Textual: the
sidebar and prompt, the transcript and its tabs, the calls replayed, and the proposals waiting
and the user's decisions on them."""

from orbit.protocol import (
    PLANNER,
    PLANNER_LANE,
    UNKNOWN,
    AgentExited,
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
from orbit.tui.view.agents import AgentRow
from tests.frontend_fakes import Shell
from tests.tui_fakes import CODER, PINNED, SNAPSHOT, STRIP, TASKS, opened

PROPOSAL = ChecksProposed(
    agent_id="agent-3", task_id="1", steps=(ProposedStep(name="Lint", command=("ruff",)),)
)
ACCEPT = DecideProposal(task_id="1", action="accept")


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
REVIEWER_TURN = (
    AgentStarted(agent_id="agent-5", role="reviewer", task_id="2"),
    AssistantMessage(turn_id="r1", agent_id="agent-5", text="Looks good."),
    TurnFinished(turn_id="r1", agent_id="agent-5", reason="completed"),
)
CRASHED_CALL = CODER_CALL[:2]
"""A coder's call whose orbit crashed before it finished: no result, and no exit."""
RESTARTED_CALL = (
    AgentStarted(agent_id="agent-3", role="coder", task_id="2"),
    ToolCallStarted(turn_id="c2", agent_id="agent-3", call_id="y", name="Read", arguments={}),
)
"""A coder's call in a run orbit started, after a restart, under the crashed run's id."""


def call_statuses(shell: Shell, key: str | None) -> list[str]:
    """The statuses of the calls and requests under the tab keyed ``key``."""
    entries = shell.window.conversation.entries(key)
    return [entry.call.status for entry in entries if entry.call is not None]


def decided_by(shell: Shell, agent: str) -> bool:
    """Whether the conversation notes a decision on ``agent``'s proposal."""
    return any(text.startswith(f"orbit → {agent}") for text, _ in shell.entries())


async def decide(shell: Shell, decision: DecideProposal | None = ACCEPT) -> None:
    """Have the user decide on the proposal shown, or close it undecided."""
    await shell.proposals.decide(decision)
    await shell.settle()


async def test_a_snapshot_shows_its_tasks_busy_planner_agents_and_steps() -> None:
    shell = Shell()
    await shell.feed(SNAPSHOT)
    assert shell.tasks.tasks == TASKS
    assert shell.task_view.usage == "session: 2k tok · $2.00"
    assert shell.marks.title == "Prompt · planner · busy"
    assert shell.marks.subtitle == "planner's model: opus · new tasks' tools: restricted"
    assert shell.agents.rows == (AgentRow("agent-3", "coder", "1", (("r1", "propose_checks"),)),)
    assert shell.agents_view.review == "review: paused"
    assert (shell.steps.steps, shell.steps.title) == (SNAPSHOT.steps, "Startup check")
    assert shell.window.title == "Conversation — auth rework"


async def test_a_read_only_snapshot_marks_the_prompt_until_one_that_isnt() -> None:
    shell = Shell()
    await shell.feed(SNAPSHOT.model_copy(update={"read_only": True}))
    assert shell.marks.title == "Prompt · planner · busy · read-only"
    await shell.feed(BusyChanged(busy=False, lane=PLANNER_LANE))
    assert shell.marks.title == "Prompt · planner · read-only"
    await shell.feed(SNAPSHOT)
    assert shell.marks.title == "Prompt · planner · busy"


async def test_a_snapshot_replaces_the_agents_and_the_transcript() -> None:
    shell = Shell()
    started = AgentStarted(agent_id="agent-1", role="planner", task_id="plan")
    await shell.feed(started, Notice(message="Workspace: /work"))
    await shell.feed(SNAPSHOT.model_copy(update={"agents": ()}))
    assert shell.agents.rows == ()
    assert shell.entries() == []
    assert shell.tab_entries(PLANNER) == []


async def test_a_snapshots_transcript_shows_its_turns_and_tool_calls_in_their_tabs() -> None:
    shell = Shell()
    await shell.feed(SNAPSHOT.model_copy(update={"transcript": logged(*PLANNER_TURN, *CODER_CALL)}))
    assert "Two tasks, then." in shell.tab_entries(PLANNER)
    assert call_statuses(shell, "coder") == ["passed"]
    assert shell.tab_entries(None)[0] == "Two tasks, then."


async def test_a_snapshots_transcript_shows_the_users_prompt_and_decision() -> None:
    shell = Shell()
    prompt = MessageSent(command_id="m", agent=PLANNER, text="add a login page")
    decision = ProposalDecided(command_id="d", agent_id="agent-3", task_id="1", action="end")
    transcript = logged(prompt, *PLANNER_TURN, CODER_CALL[0], decision)
    await shell.feed(SNAPSHOT.model_copy(update={"transcript": transcript}))
    assert shell.tab_entries(PLANNER)[:2] == ["add a login page", "Two tasks, then."]
    assert shell.tab_entries(None)[0] == "add a login page"
    assert shell.tab_entries(None)[-1] == "orbit → agent-3: task ended"
    assert shell.tab_entries("coder")[-1] == "orbit → agent-3: task ended"


async def test_a_later_snapshot_of_another_session_replaces_the_transcript() -> None:
    shell = Shell()
    other = opened(SessionSummary(id="s2", name="docs"))
    note = Notice(message="Notes on docs.")
    await shell.feed(SNAPSHOT.model_copy(update={"transcript": logged(*PLANNER_TURN, *CODER_CALL)}))
    await shell.feed(other.model_copy(update={"transcript": logged(note)}))
    assert shell.tab_entries(None) == ["📌 Notes on docs."]
    assert shell.tab_entries(PLANNER) == []
    assert shell.tab_entries("coder") == []
    assert shell.window.title == "Conversation — docs"


async def test_a_snapshot_closes_the_agent_tabs_its_transcript_doesnt_open() -> None:
    shell = Shell()
    checker = AgentStarted(agent_id="agent-4", role="checker", task_id="1")
    running = RunningAgent(agent_id="agent-4", role="checker", task_id="1")
    empty = opened(SessionSummary(id="s1", name="auth"))
    await shell.feed(checker)
    assert shell.tab_strip == [*STRIP, "checker"]
    await shell.feed(empty)
    assert shell.tab_strip == STRIP
    await shell.feed(empty.model_copy(update={"transcript": logged(checker), "agents": (running,)}))
    assert shell.tab_strip == [*STRIP, "checker"]


async def test_a_snapshots_agents_started_but_not_running_get_no_tab_or_marker() -> None:
    shell = Shell()
    checker = AgentStarted(agent_id="agent-4", role="checker", task_id="1")
    crashed = opened(SessionSummary(id="s1", name="auth")).model_copy(
        update={"transcript": logged(*CODER_CALL, checker)}
    )
    await shell.feed(crashed)
    assert shell.tab_strip == STRIP
    assert "Read" in "".join(shell.tab_entries("coder"))


async def test_a_running_agent_started_before_the_transcript_gets_its_roles_tab() -> None:
    shell = Shell()
    checker = RunningAgent(agent_id="agent-4", role="checker", task_id="2")
    cut = SNAPSHOT.model_copy(
        update={"agents": (CODER, checker), "transcript": logged(*CODER_CALL[1:])}
    )
    await shell.feed(cut)
    assert shell.tab_strip == ["All", "● planner", "● coder", *PINNED[2:], "checker"]
    assert call_statuses(shell, "coder") == ["passed"]
    assert shell.tab_entries("agent-3") == []


async def test_a_snapshots_transcript_marks_no_tab_unread() -> None:
    shell = Shell()
    transcript = logged(TaskStarted(task_id="1"), *PLANNER_TURN, *CODER_CALL, *REVIEWER_TURN)
    await shell.feed(SNAPSHOT.model_copy(update={"transcript": transcript}))
    assert shell.tab_strip == ["All", "● planner", "● coder", *PINNED[2:]]
    assert "Looks good." in shell.tab_entries("reviewer")


async def test_a_live_event_after_a_snapshot_marks_its_tab_unread() -> None:
    shell = Shell()
    said = AssistantMessage(turn_id="c2", agent_id="agent-3", text="Done.")
    await shell.feed(SNAPSHOT.model_copy(update={"transcript": logged(*CODER_CALL)}))
    await shell.feed(said, TaskStarted(task_id="1"))
    assert shell.tab_strip == ["All", "● planner", "● coder *", *PINNED[2:]]


async def test_a_replayed_call_of_an_agent_not_running_shows_no_result() -> None:
    shell = Shell()
    await shell.feed(
        SNAPSHOT.model_copy(update={"agents": (), "transcript": logged(*CRASHED_CALL)})
    )
    assert call_statuses(shell, "coder") == ["no_result"]
    assert call_statuses(shell, None) == ["no_result"]
    assert not shell.window.ticking


async def test_a_replayed_call_of_a_running_agent_stays_running() -> None:
    shell = Shell()
    await shell.feed(SNAPSHOT.model_copy(update={"transcript": logged(*CRASHED_CALL)}))
    assert call_statuses(shell, "coder") == ["running"]
    assert shell.window.ticking


async def test_a_replayed_request_of_an_agent_not_running_shows_no_result() -> None:
    shell = Shell()
    request = AgentRequest(agent_id="agent-3", request_id="r1", tool="get_task", arguments={})
    transcript = logged(CODER_CALL[0], request)
    await shell.feed(SNAPSHOT.model_copy(update={"agents": (), "transcript": transcript}))
    assert call_statuses(shell, "coder") == ["no_result"]
    assert not shell.window.ticking


async def test_a_replayed_call_of_the_planner_listed_running_stays_running() -> None:
    shell = Shell()
    planner = RunningAgent(agent_id="agent-1", role=PLANNER, task_id="plan")
    started = AgentStarted(agent_id="agent-1", role=PLANNER, task_id="plan")
    call = ToolCallStarted(turn_id="p1", agent_id="agent-1", call_id="y", name="Read", arguments={})
    transcript = logged(started, call, *CRASHED_CALL)
    await shell.feed(
        SNAPSHOT.model_copy(update={"agents": (planner, CODER), "transcript": transcript})
    )
    assert call_statuses(shell, PLANNER) == ["running"]
    assert call_statuses(shell, "coder") == ["running"]


async def test_a_replayed_call_of_an_earlier_run_under_a_running_agents_id_shows_no_result() -> (
    None
):
    shell = Shell()
    await shell.feed(
        SNAPSHOT.model_copy(update={"transcript": logged(*CRASHED_CALL, *RESTARTED_CALL)})
    )
    assert call_statuses(shell, "coder") == ["no_result", "running"]
    assert call_statuses(shell, None) == ["no_result", "running"]


async def test_a_replayed_request_of_an_earlier_run_under_a_running_agents_id_shows_no_result() -> (
    None
):
    shell = Shell()
    request = AgentRequest(agent_id="agent-3", request_id="r0", tool="get_task", arguments={})
    transcript = logged(CODER_CALL[0], request, *RESTARTED_CALL)
    await shell.feed(SNAPSHOT.model_copy(update={"transcript": transcript}))
    assert call_statuses(shell, "coder") == ["no_result", "running"]
    assert call_statuses(shell, None) == ["no_result", "running"]


async def test_an_agent_starting_under_an_id_ends_the_earlier_runs_calls_live() -> None:
    shell = Shell()
    await shell.feed(SNAPSHOT.model_copy(update={"agents": ()}))
    await shell.feed(*CRASHED_CALL)
    assert shell.window.ticking
    await shell.feed(RESTARTED_CALL[0])
    assert call_statuses(shell, "coder") == ["no_result"]
    assert not shell.window.ticking


async def test_a_snapshots_transcript_shows_a_reset_and_a_refusal_as_they_were_shown() -> None:
    shell = Shell()
    refused = CommandRefused(command_id="c", code="busy", message="Task 1 is running.")
    tail = logged(ConversationReset(agent=PLANNER), refused, CommandAccepted(command_id="d"))
    await shell.feed(ConversationReset(agent=PLANNER), refused)
    live = shell.entries()
    await shell.feed(SNAPSHOT.model_copy(update={"transcript": tail}))
    assert shell.entries() == live


async def test_a_snapshot_with_a_waiting_proposal_shows_it() -> None:
    shell = Shell()
    await shell.feed(SNAPSHOT.model_copy(update={"proposals": (PROPOSAL,)}))
    assert shell.proposals.shown == PROPOSAL


async def test_a_snapshot_still_waiting_on_the_shown_proposal_keeps_it() -> None:
    shell = Shell()
    await shell.feed(PROPOSAL)
    await shell.feed(SNAPSHOT.model_copy(update={"proposals": (PROPOSAL,)}))
    assert (shell.proposals.opened, shell.proposals.closed) == ([PROPOSAL], [])


async def test_a_snapshot_without_the_shown_proposal_closes_it() -> None:
    shell = Shell()
    await shell.feed(PROPOSAL)
    await shell.feed(SNAPSHOT)
    assert shell.proposals.shown is None
    assert shell.wire.commands == []


async def test_a_snapshot_with_another_proposal_shows_that_one_in_place_of_the_shown() -> None:
    shell = Shell()
    other = PROPOSAL.model_copy(update={"task_id": "2"})
    await shell.feed(PROPOSAL)
    await shell.feed(SNAPSHOT.model_copy(update={"proposals": (other,)}))
    assert shell.proposals.shown == other
    await decide(shell)
    assert shell.proposals.shown is None


async def test_the_proposal_event_after_a_snapshot_showing_it_shows_it_once() -> None:
    shell = Shell()
    await shell.feed(SNAPSHOT.model_copy(update={"proposals": (PROPOSAL,)}))
    await shell.feed(PROPOSAL)
    assert shell.proposals.opened == [PROPOSAL]
    await decide(shell)
    assert shell.wire.commands == [ACCEPT]
    assert shell.proposals.shown is None


async def test_another_proposal_event_while_one_is_shown_waits_for_it() -> None:
    shell = Shell()
    other = PROPOSAL.model_copy(update={"task_id": "2"})
    await shell.feed(SNAPSHOT.model_copy(update={"proposals": (PROPOSAL,)}))
    await shell.feed(other)
    assert shell.proposals.shown == PROPOSAL
    await decide(shell)
    assert shell.proposals.shown == other


async def test_a_refused_decision_drops_its_entry_and_shows_the_proposal_again() -> None:
    shell = Shell()
    await shell.feed(PROPOSAL)
    await decide(shell)
    assert shell.proposals.shown is None
    await shell.refuse("busy", "Not now.")
    assert shell.entries()[-1] == ("Not now.", "error")
    assert not decided_by(shell, "agent-3")
    assert shell.proposals.shown == PROPOSAL


async def test_two_decisions_in_flight_each_hear_their_own_outcome() -> None:
    shell = Shell()
    other = PROPOSAL.model_copy(update={"agent_id": "agent-4", "task_id": "2"})
    await shell.feed(PROPOSAL, other)
    await decide(shell)
    await decide(shell, DecideProposal(task_id="2", action="accept"))
    first, second = shell.wire.sent
    refused = CommandRefused(command_id=first.command_id, code="busy", message="Not now.")
    await shell.feed(CommandAccepted(command_id=second.command_id), refused)
    assert not decided_by(shell, "agent-3")
    assert decided_by(shell, "agent-4")
    assert shell.proposals.shown == PROPOSAL


async def test_a_decision_refused_as_unknown_drops_its_entry_and_leaves_the_proposal_closed() -> (
    None
):
    shell = Shell()
    await shell.feed(PROPOSAL)
    await decide(shell)
    message = "No proposal is waiting for task 1."
    await shell.refuse(UNKNOWN, message)
    assert shell.entries()[-1] == (message, "error")
    assert not decided_by(shell, "agent-3")
    assert shell.proposals.shown is None


async def test_a_decision_whose_agent_exits_before_its_refusal_leaves_the_proposal_closed() -> None:
    shell = Shell()
    await shell.feed(PROPOSAL)
    await decide(shell)
    sent = shell.wire.sent[-1].command_id
    refused = CommandRefused(command_id=sent, code="busy", message="Not now.")
    await shell.feed(AgentExited(agent_id="agent-3", reason="killed"), refused)
    assert not decided_by(shell, "agent-3")
    assert shell.proposals.shown is None


async def test_a_refused_decision_reopens_a_new_proposal_from_a_reused_agent_id() -> None:
    shell = Shell()
    await shell.feed(AgentExited(agent_id="agent-3", reason="finished"), PROPOSAL)
    await decide(shell)
    await shell.refuse("busy", "Not now.")
    assert shell.proposals.shown == PROPOSAL


async def test_a_refused_decision_reopens_a_snapshot_proposal_from_a_reused_agent_id() -> None:
    shell = Shell()
    waiting = SNAPSHOT.model_copy(update={"proposals": (PROPOSAL,)})
    await shell.feed(AgentExited(agent_id="agent-3", reason="finished"), waiting)
    await decide(shell)
    await shell.refuse("busy", "Not now.")
    assert shell.proposals.shown == PROPOSAL


async def test_a_decision_that_failed_to_send_drops_its_entry_and_shows_the_proposal_again() -> (
    None
):
    shell = Shell()
    shell.wire.send_error = RuntimeError("the backend is gone")
    await shell.feed(PROPOSAL)
    await decide(shell)
    assert shell.entries() == [("Couldn't send the command: the backend is gone", "error")]
    assert shell.proposals.shown == PROPOSAL


async def test_an_accepted_decision_keeps_its_entry_and_shows_the_next_proposal() -> None:
    shell = Shell()
    other = PROPOSAL.model_copy(update={"task_id": "2"})
    await shell.feed(PROPOSAL, other)
    await decide(shell)
    await shell.accept_last()
    assert shell.entries() == [("orbit → agent-3: proposal accepted", "agent")]
    assert shell.proposals.shown == other
