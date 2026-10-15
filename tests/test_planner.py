"""Planning in conversation: prompts go to the planner, whose proposals become draft tasks."""

import asyncio

from orbit.agent import AgentSession
from orbit.agent.domain.roles import BUILT_IN_ROLES
from orbit.protocol import (
    AcceptTask,
    AddTask,
    AgentExited,
    AgentStarted,
    BusyChanged,
    ErrorOccurred,
    Event,
    ProgressNoted,
    RemoveTask,
    RunTasks,
    SubmitPrompt,
    TasksChanged,
    TaskSpec,
    TaskState,
    TurnEndReason,
    TurnStarted,
)
from tests.agent_fakes import Call, session_for


async def until_idle(session: AgentSession) -> list[Event]:
    events: list[Event] = []
    async for event in session.events():
        events.append(event)
        if isinstance(event, BusyChanged) and not event.busy:
            return events
    raise AssertionError("unreachable")


def board(events: list[Event]) -> tuple[TaskState, ...]:
    return [e for e in events if isinstance(e, TasksChanged)][-1].tasks


async def proposes_two(call: Call) -> TurnEndReason:
    first = await call("propose_task", title="Add the flag", prompt="Add --fast to the CLI.")
    assert "draft" in first.text
    await call("propose_task", title="Document it", prompt="Mention --fast in the README.")
    return "completed"


async def chats(call: Call) -> TurnEndReason:
    return "completed"


async def plan(session: AgentSession, text: str) -> list[Event]:
    await session.send(SubmitPrompt(text=text))
    return await asyncio.wait_for(until_idle(session), 5)


async def test_prompts_to_the_planner_go_to_it_and_proposals_become_drafts() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [proposes_two, chats]
    events = await plan(session, "I want a --fast flag.")
    await plan(session, "Thanks.")

    assert BusyChanged(busy=True, lane="planner") in events
    assert events[-1] == BusyChanged(busy=False, lane="planner")
    first, second = launcher.launches
    assert (first.agent.id, first.agent.role, first.prompt) == (
        "agent-1",
        "planner",
        "I want a --fast flag.",
    )
    assert second.agent.id == "agent-1"  # The same conversation, resumed.
    assert first.tools == BUILT_IN_ROLES["planner"].tools and first.cwd == "."
    drafts = board(events)
    assert [(t.status, t.origin, t.task.prompt) for t in drafts] == [
        ("draft", "planner", "Add the flag\n\nAdd --fast to the CLI."),
        ("draft", "planner", "Document it\n\nMention --fast in the README."),
    ]
    assert drafts[0].task.settings.tool_access == "full"  # The coder role's default.
    assert any(isinstance(e, TurnStarted) and e.agent_id == "agent-1" for e in events)


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
    await session.send(SubmitPrompt(text="Plan."))
    stream = session.events()
    return [await anext(stream), await anext(stream), await anext(stream)]


async def test_planning_needs_agents() -> None:
    events = await refused_planning(AgentSession())
    assert any(isinstance(e, ErrorOccurred) and "planner" in e.message for e in events)
    assert events[-1] == BusyChanged(busy=False, lane="planner")  # Frees the prompt.


async def test_planning_needs_a_planner_role() -> None:
    roles = {name: role for name, role in BUILT_IN_ROLES.items() if name != "planner"}
    session, _, _ = session_for(roles=roles)
    events = await refused_planning(session)
    assert any(isinstance(e, ErrorOccurred) and "planner" in e.message for e in events)
    assert events[-1] == BusyChanged(busy=False, lane="planner")


async def test_accepted_drafts_run_and_dropped_ones_go() -> None:
    async def done(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("complete_task", summary="ok")
        return "completed"

    session, _, launcher = session_for(done)
    launcher.planner_turns = [proposes_two]
    await plan(session, "Plan it.")
    await session.send(AcceptTask(task_id="1"))
    await session.send(RemoveTask(task_id="2"))
    await session.send(RunTasks())
    events = await asyncio.wait_for(until_idle(session), 5)
    assert [(t.id, t.status) for t in board(events)] == [("1", "completed")]


async def test_draft_and_removal_mistakes_are_reported() -> None:
    gate_open = asyncio.Event()

    async def waits(call: Call) -> TurnEndReason:
        await call("get_task")
        gate_open.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, _ = session_for(waits)
    await session.send(AddTask(task=TaskSpec(prompt="one")))
    await session.send(AcceptTask(task_id="1"))
    await session.send(RunTasks())
    await asyncio.wait_for(gate_open.wait(), 5)
    await session.send(RemoveTask(task_id="1"))
    await session.send(AcceptTask(task_id="9"))
    errors: list[str] = []
    async for event in session.events():
        if isinstance(event, ErrorOccurred):
            errors.append(event.message)
        if len(errors) == 3:
            break
    assert errors == [
        "Task '1' isn't a draft.",
        "Task '1' can't be removed while it runs.",
        "No task with id '9'.",
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


async def test_closing_ends_the_planners_access() -> None:
    session, host, launcher = session_for()
    launcher.planner_turns = [chats]
    await plan(session, "Plan.")
    await session.close()
    reply = await host.call("token-1", "propose_task", title="x", prompt="y")
    assert reply.is_error
    exited = AgentExited(agent_id="agent-1", reason="finished")
    assert await asyncio.wait_for(anext(session.events()), 5) == exited
