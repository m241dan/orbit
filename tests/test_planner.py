"""Planning in conversation: prompts go to the planner, whose proposals become draft tasks."""

import asyncio

import pytest

from orbit.agent import Agents, AgentSession
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.planner import RESUME_FAILED_NOTE, PlannerChat
from orbit.agent.application.ports import Reply, ResumeFailed
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.verification import steps_note
from orbit.protocol import (
    BUSY,
    INVALID,
    NOT_CONFIGURED,
    PLAN_TASK,
    PLANNER,
    UNKNOWN,
    AcceptTask,
    AddTask,
    AgentExited,
    AgentStarted,
    BusyChanged,
    CommandRefused,
    ErrorOccurred,
    Event,
    Notice,
    ProgressNoted,
    RemoveTask,
    RunTasks,
    SendMessage,
    Settings,
    TasksChanged,
    TaskSpec,
    TurnEndReason,
    TurnFinished,
    TurnStarted,
)
from tests.agent_fakes import (
    ROLES,
    Call,
    FakeHost,
    Script,
    ScriptedLauncher,
    board,
    events_until,
    idle,
    payloads,
    refused,
    send,
    session_for,
)


async def until_idle(session: AgentSession) -> list[Event]:
    return await events_until(session, idle(lane=None))


async def proposes_two(call: Call) -> TurnEndReason:
    first = await call("propose_task", title="Add the flag", prompt="Add --fast to the CLI.")
    assert "draft" in first.text
    await call("propose_task", title="Document it", prompt="Mention --fast in the README.")
    return "completed"


async def chats(call: Call) -> TurnEndReason:
    return "completed"


async def plan(session: AgentSession, text: str) -> list[Event]:
    await send(session, SendMessage(agent=PLANNER, text=text))
    return await until_idle(session)


async def test_prompts_to_the_planner_go_to_it_and_proposals_become_drafts() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [proposes_two, chats]
    events = await plan(session, "I want a --fast flag.")
    await plan(session, "Thanks.")

    assert BusyChanged(busy=True, lane="planner") in events
    assert events[-1] == BusyChanged(busy=False, lane="planner")
    first, second = launcher.launches
    assert (first.agent.id, first.agent.role) == ("agent-1", "planner")
    assert first.prompt == f"{steps_note(())}\n\nI want a --fast flag."
    assert second.agent.id == "agent-1"  # The same conversation, resumed.
    assert second.prompt == "Thanks."
    assert first.tools == ROLES["planner"].tools and first.cwd == "."
    drafts = board(events)
    assert [(t.status, t.origin, t.task.prompt) for t in drafts] == [
        ("draft", "planner", "Add the flag\n\nAdd --fast to the CLI."),
        ("draft", "planner", "Document it\n\nMention --fast in the README."),
    ]
    assert drafts[0].task.settings.tool_access == "full"  # The coder role's default.
    assert any(isinstance(e, TurnStarted) and e.agent_id == "agent-1" for e in events)


async def test_a_proposal_without_a_title_is_refused_and_adds_no_draft() -> None:
    replies: list[Reply] = []

    async def proposes_untitled(call: Call) -> TurnEndReason:
        replies.append(await call("propose_task", title=" \n", prompt="Do X."))
        return "completed"

    session, _, launcher = session_for()
    launcher.planner_turns = [proposes_untitled]
    events = await plan(session, "Plan it.")
    assert replies == [Reply("A task needs a title.", is_error=True)]
    assert not any(isinstance(e, TasksChanged) for e in events)


async def test_the_first_turn_says_which_agent_is_the_planner() -> None:
    # So the frontend can put the planner's output on the Planner tab.
    session, _, launcher = session_for()
    launcher.planner_turns = [chats, chats]
    first = await plan(session, "Plan.")
    later = await plan(session, "More.")
    started = AgentStarted(agent_id="agent-1", role="planner", task_id="plan")
    assert [e for e in first if isinstance(e, AgentStarted)] == [started]
    assert not any(isinstance(e, AgentStarted) for e in later)


async def refused_planning(session: AgentSession) -> list[Event]:
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    return await events_until(session, lambda e: isinstance(e, CommandRefused))


NO_PLANNER = refused(NOT_CONFIGURED, "Planning needs agents and a planner role.")


async def test_planning_needs_agents() -> None:
    events = await refused_planning(AgentSession())
    assert events[-1] == NO_PLANNER
    assert BusyChanged(busy=False, lane="planner") in events  # Frees the prompt.
    assert not any(isinstance(e, ErrorOccurred) for e in events)


async def test_planning_needs_a_planner_role() -> None:
    roles = {name: role for name, role in ROLES.items() if name != "planner"}
    session, _, _ = session_for(roles=roles)
    events = await refused_planning(session)
    assert events[-1] == NO_PLANNER
    assert BusyChanged(busy=False, lane="planner") in events


async def test_accepted_drafts_run_and_dropped_ones_go() -> None:
    async def done(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("complete_task", summary="ok")
        return "completed"

    session, _, launcher = session_for(done)
    launcher.planner_turns = [proposes_two]
    await plan(session, "Plan it.")
    await send(session, AcceptTask(task_id="1"))
    await send(session, RemoveTask(task_id="2"))
    await send(session, RunTasks())
    events = await until_idle(session)
    assert [(t.id, t.status) for t in board(events)] == [("1", "completed")]


async def test_draft_and_removal_mistakes_are_reported() -> None:
    gate_open = asyncio.Event()

    async def waits(call: Call) -> TurnEndReason:
        await call("get_task")
        gate_open.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, _ = session_for(waits)
    await send(session, AddTask(task=TaskSpec(prompt="one")))
    await send(session, AcceptTask(task_id="1"))
    await send(session, RunTasks())
    await asyncio.wait_for(gate_open.wait(), 5)
    await send(session, RemoveTask(task_id="1"))
    await send(session, AcceptTask(task_id="9"))
    refusals: list[CommandRefused] = []
    async for event in payloads(session):
        assert not isinstance(event, ErrorOccurred)
        if isinstance(event, CommandRefused):
            refusals.append(event)
        if len(refusals) == 3:
            break
    assert refusals == [
        refused(INVALID, "Task '1' isn't a draft."),
        refused(BUSY, "Task '1' can't be removed while it runs."),
        refused(UNKNOWN, "No task with id '9'."),
    ]
    await session.close()


async def test_the_planner_may_only_use_its_roles_tools_and_can_note_progress() -> None:
    async def oversteps(call: Call) -> TurnEndReason:
        refused = await call("get_task")
        assert refused.is_error and "planner" in refused.text
        assert (await call("report_progress", note="reading the CLI")).text == "Noted."
        return "completed"

    session, _, launcher = session_for()
    launcher.planner_turns = [oversteps]
    events = await plan(session, "Plan.")
    assert ProgressNoted(agent_id="agent-1", task_id="plan", note="reading the CLI") in events


async def test_the_planners_long_notes_are_cut_short_like_any_agents() -> None:
    async def chatty(call: Call) -> TurnEndReason:
        await call("report_progress", note="  " + "x" * 300 + "  ")
        return "completed"

    session, _, launcher = session_for()
    launcher.planner_turns = [chatty]
    events = await plan(session, "Plan.")
    notes = [e.note for e in events if isinstance(e, ProgressNoted)]
    assert notes == ["x" * 199 + "…"]
    await session.close()


async def test_closing_ends_the_planners_access() -> None:
    session, host, launcher = session_for()
    launcher.planner_turns = [chats]
    await plan(session, "Plan.")
    await session.close()
    reply = await host.call("token-1", "propose_task", title="x", prompt="y")
    assert reply.is_error
    exited = AgentExited(agent_id="agent-1", reason="finished")
    assert await asyncio.wait_for(anext(payloads(session)), 5) == exited


async def planner_model(role_model: str | None, picked: str | None) -> str | None:
    """The model the planner launches on, with ``role_model`` in its role and ``picked`` chosen."""
    host = FakeHost()
    launcher = ScriptedLauncher(host)
    launcher.planner_turns = [chats]
    roles = {**ROLES, "planner": ROLES["planner"].model_copy(update={"model": role_model})}

    async def publish(event: Event) -> None:
        pass

    desk = AgentDesk(Agents(host, launcher, lambda: "token-1", roles), publish)
    chat = PlannerChat(desk, Tasks(TaskBoard(), publish), lambda: ())
    async for _ in chat.run_turn("turn", "Plan.", Settings(model=picked)):
        pass
    return launcher.launches[-1].settings.model


async def test_the_planner_uses_its_roles_model_when_none_is_picked() -> None:
    assert await planner_model("sonnet", None) == "sonnet"


async def test_a_picked_model_wins_over_the_planner_roles() -> None:
    assert await planner_model("sonnet", "opus") == "opus"


async def test_the_planner_uses_the_default_model_when_neither_names_one() -> None:
    assert await planner_model(None, None) is None


def steps_chat(
    *turns: Script, published: list[Event] | None = None
) -> tuple[PlannerChat, ScriptedLauncher, list[tuple[str, ...]]]:
    """A planner chat playing ``turns``, whose steps are the last in the list it returns; what
    it publishes goes to ``published``."""
    host = FakeHost()
    launcher = ScriptedLauncher(host)
    launcher.planner_turns = list(turns)
    sink = published if published is not None else []

    async def publish(event: Event) -> None:
        sink.append(event)

    desk = AgentDesk(Agents(host, launcher, lambda: "token-1", ROLES), publish)
    steps: list[tuple[str, ...]] = [("Ruff lint",)]
    return PlannerChat(desk, Tasks(TaskBoard(), publish), lambda: steps[-1]), launcher, steps


async def turn(chat: PlannerChat, launcher: ScriptedLauncher, text: str) -> str:
    async for _ in chat.run_turn("turn", text, Settings()):
        pass
    return launcher.launches[-1].prompt


async def fails(call: Call) -> TurnEndReason:
    raise RuntimeError("Could not start 'claude'.")


async def errs(call: Call) -> TurnEndReason:
    return "error"


async def hangs(call: Call) -> TurnEndReason:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


async def test_the_planner_hears_the_steps_on_its_first_turn_and_when_they_change() -> None:
    chat, launcher, steps = steps_chat(chats, chats, chats)
    first = await turn(chat, launcher, "Plan a flag.")
    assert first.startswith(steps_note(("Ruff lint",))) and first.endswith("\n\nPlan a flag.")
    assert await turn(chat, launcher, "And docs.") == "And docs."
    steps.append(("Ruff lint", "Unit tests"))
    more = await turn(chat, launcher, "More.")
    assert more == f"{steps_note(('Ruff lint', 'Unit tests'))}\n\nMore."


async def test_the_planner_hears_the_steps_again_after_a_turn_that_raised() -> None:
    chat, launcher, _ = steps_chat(fails, chats)
    with pytest.raises(RuntimeError):
        await turn(chat, launcher, "Plan a flag.")
    again = await turn(chat, launcher, "Again.")
    assert again == f"{steps_note(('Ruff lint',))}\n\nAgain."


async def test_the_planner_hears_the_steps_again_after_a_turn_that_ended_in_error() -> None:
    chat, launcher, _ = steps_chat(errs, chats)
    await turn(chat, launcher, "Plan a flag.")
    again = await turn(chat, launcher, "Again.")
    assert again == f"{steps_note(('Ruff lint',))}\n\nAgain."


async def test_the_planner_hears_the_steps_again_after_a_cancelled_turn() -> None:
    chat, launcher, _ = steps_chat(hangs, chats)
    first = asyncio.create_task(turn(chat, launcher, "Plan a flag."))
    while not launcher.launches:
        await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    again = await turn(chat, launcher, "Again.")
    assert again == f"{steps_note(('Ruff lint',))}\n\nAgain."


async def test_the_planners_agent_id_is_known_once_its_first_turn_opens_it() -> None:
    chat, launcher, _ = steps_chat(chats)
    assert chat.agent_id is None
    await turn(chat, launcher, "Plan.")
    assert chat.agent_id == "agent-1"


def turn_id_of(published: list[Event]) -> str:
    started = next(e for e in published if isinstance(e, TurnStarted))
    return started.turn_id


async def test_answering_publishes_a_completed_turn_and_returns_completed() -> None:
    published: list[Event] = []
    chat, _, _ = steps_chat(chats, published=published)
    assert await chat.answer("Plan.", Settings()) == "completed"
    turn_id = turn_id_of(published)
    assert published == [
        AgentStarted(agent_id="agent-1", role="planner", task_id=PLAN_TASK),
        TurnStarted(turn_id=turn_id, agent_id="agent-1"),
        TurnFinished(turn_id=turn_id, agent_id="agent-1", reason="completed"),
    ]


async def test_answering_a_cancelled_turn_publishes_it_cancelled_and_returns_cancelled() -> None:
    published: list[Event] = []
    chat, launcher, _ = steps_chat(hangs, published=published)
    answering = asyncio.create_task(chat.answer("Plan.", Settings()))
    while not launcher.launches:
        await asyncio.sleep(0)
    answering.cancel()
    assert await answering == "cancelled"
    finished = TurnFinished(turn_id=turn_id_of(published), agent_id="agent-1", reason="cancelled")
    assert published[-1] == finished


async def test_answering_a_failed_turn_publishes_the_error_and_returns_error() -> None:
    published: list[Event] = []
    chat, _, _ = steps_chat(fails, published=published)
    assert await chat.answer("Plan.", Settings()) == "error"
    turn_id = turn_id_of(published)
    message = "Could not start 'claude'."
    assert published[-2:] == [
        ErrorOccurred(turn_id=turn_id, agent_id="agent-1", message=message),
        TurnFinished(turn_id=turn_id, agent_id="agent-1", reason="error"),
    ]


async def blocks() -> None:
    await asyncio.Event().wait()


async def breaks() -> None:
    raise RuntimeError("Could not save the session.")


def finished_turns(published: list[Event]) -> list[Event]:
    return [e for e in published if isinstance(e, TurnFinished)]


async def test_cancelling_a_turn_after_it_finished_publishes_one_finish_and_returns_it() -> None:
    published: list[Event] = []
    chat, launcher, _ = steps_chat(chats, published=published)
    launcher.planner_replies.append("Here is a plan.")
    launcher.planner_afters.append(blocks)
    answering = asyncio.create_task(chat.answer("Plan.", Settings()))
    while not finished_turns(published):
        await asyncio.sleep(0)
    answering.cancel()
    assert await answering == "completed"
    finished = TurnFinished(turn_id=turn_id_of(published), agent_id="agent-1", reason="completed")
    assert finished_turns(published) == [finished]


async def test_a_turn_failing_after_it_finished_reports_the_error_but_no_second_finish() -> None:
    published: list[Event] = []
    chat, launcher, _ = steps_chat(chats, published=published)
    launcher.planner_afters.append(breaks)
    assert await chat.answer("Plan.", Settings()) == "completed"
    turn_id = turn_id_of(published)
    message = "Could not save the session."
    assert published[-2:] == [
        TurnFinished(turn_id=turn_id, agent_id="agent-1", reason="completed"),
        ErrorOccurred(turn_id=turn_id, agent_id="agent-1", message=message),
    ]


async def resume_lost(call: Call) -> TurnEndReason:
    raise ResumeFailed("No conversation found with session ID: saved-1")


async def test_the_conversation_id_is_known_once_a_turn_starts_it() -> None:
    chat, launcher, _ = steps_chat(chats)
    assert chat.conversation_id is None
    await turn(chat, launcher, "Plan.")
    assert chat.conversation_id == "conversation-agent-1"
    assert launcher.resumed == [None]


async def test_a_restored_conversation_is_carried_on_from_the_first_turn() -> None:
    chat, launcher, _ = steps_chat(chats, chats)
    chat.restore("saved-1")
    assert chat.conversation_id == "saved-1"
    await turn(chat, launcher, "Carry on.")
    await turn(chat, launcher, "And more.")
    assert launcher.resumed == ["saved-1", "saved-1"]
    assert chat.conversation_id == "saved-1"


async def test_a_conversation_that_cannot_be_resumed_starts_fresh_on_the_same_prompt() -> None:
    published: list[Event] = []
    chat, launcher, _ = steps_chat(resume_lost, chats, published=published)
    chat.restore("saved-1")
    assert await chat.answer("Carry on.", Settings()) == "completed"
    turn_id = turn_id_of(published)
    assert published == [
        AgentStarted(agent_id="agent-1", role="planner", task_id=PLAN_TASK),
        TurnStarted(turn_id=turn_id, agent_id="agent-1"),
        Notice(message=RESUME_FAILED_NOTE),
        TurnFinished(turn_id=turn_id, agent_id="agent-1", reason="completed"),
    ]
    assert launcher.resumed == ["saved-1", None]
    fresh = launcher.launches[-1].prompt
    assert fresh == f"{steps_note(('Ruff lint',))}\n\nCarry on."
    assert chat.conversation_id == "conversation-agent-1"


async def test_once_the_saved_conversation_resumed_failures_to_resume_are_not_retried() -> None:
    chat, launcher, _ = steps_chat(chats, resume_lost, chats)
    chat.restore("saved-1")
    await turn(chat, launcher, "Plan.")
    with pytest.raises(ResumeFailed):
        await turn(chat, launcher, "More.")
    assert launcher.resumed == ["saved-1", "saved-1"]


async def test_starting_fresh_is_tried_once() -> None:
    published: list[Event] = []
    chat, launcher, _ = steps_chat(resume_lost, fails, published=published)
    chat.restore("saved-1")
    assert await chat.answer("Carry on.", Settings()) == "error"
    assert len(launcher.launches) == 2
    assert Notice(message=RESUME_FAILED_NOTE) in published
    assert finished_turns(published)[-1] == TurnFinished(
        turn_id=turn_id_of(published), agent_id="agent-1", reason="error"
    )


async def test_a_failure_to_resume_without_a_saved_conversation_is_not_retried() -> None:
    published: list[Event] = []
    chat, launcher, _ = steps_chat(chats, resume_lost, published=published)
    await turn(chat, launcher, "Plan.")
    assert await chat.answer("More.", Settings()) == "error"
    assert len(launcher.launches) == 2
    assert not any(isinstance(e, Notice) for e in published)


async def test_a_cancelled_first_turn_still_falls_back_if_the_saved_one_is_gone() -> None:
    chat, launcher, _ = steps_chat(hangs, resume_lost, chats)
    chat.restore("saved-1")
    first = asyncio.create_task(turn(chat, launcher, "Plan."))
    while not launcher.launches:
        await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await turn(chat, launcher, "Again.")
    assert launcher.resumed == ["saved-1", "saved-1", None]
