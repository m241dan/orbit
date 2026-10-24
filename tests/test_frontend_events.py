"""What the frontend shows of the backend's events, without Textual: the Hello it opens with,
what shows in the conversation, and what shows nothing there."""

import pytest

from orbit.protocol import (
    INTERNAL,
    PLANNER,
    PROTOCOL_VERSION,
    AgentReply,
    AgentStarted,
    AssistantMessage,
    BusyChanged,
    ErrorOccurred,
    Event,
    Hello,
    Notice,
    SendMessage,
    SessionStateChanged,
    StepState,
    TaskMerged,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskState,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    UsageChanged,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
    Welcome,
)
from orbit.tui.view.events import coalesce
from tests.frontend_fakes import Shell


async def test_the_frontend_opens_with_a_hello_from_the_tui() -> None:
    shell = Shell()
    assert await shell.frontend.hello()
    await shell.submit("hi")
    first, second = shell.wire.sent
    assert isinstance(first.payload, Hello)
    assert first.payload.client.startswith("orbit-tui ")
    assert first.payload.protocol_version == PROTOCOL_VERSION == 2
    assert first.payload.since is None
    assert second.payload == SendMessage(agent=PLANNER, text="hi")


async def test_a_welcome_and_a_sessions_state_show_nothing_in_the_conversation() -> None:
    shell = Shell()
    change = SessionStateChanged(session="s1", active=True, state="running", viewers=2)
    await shell.feed(Welcome(command_id="hello", server_version="1.0"), change)
    assert shell.entries() == []


async def test_tool_calls_errors_and_a_stop_show_in_the_conversation() -> None:
    shell = Shell()
    await shell.feed(
        ToolCallStarted(turn_id="t", call_id="c1", name="upper", arguments={"text": "hi"}),
        ToolCallFinished(turn_id="t", call_id="c1", output="HI"),
        ToolCallFinished(turn_id="t", call_id="c2", output="KeyError", is_error=True),
        ErrorOccurred(code=INTERNAL, message="boom"),
        TurnFinished(turn_id="t", reason="cancelled"),
    )
    assert shell.entries() == [
        ('→ upper {"text": "hi"} ✓ 1 line', "tool"),
        ("KeyError", "error"),
        ("boom", "error"),
        ("stopped", "rule"),
    ]


async def test_agents_starting_replies_and_notices_show_in_the_conversation() -> None:
    shell = Shell()
    await shell.feed(
        AgentStarted(agent_id="agent-1", role="coder", task_id="1"),
        AgentReply(agent_id="agent-1", request_id="r", text="Do X."),
        Notice(message="1 stash"),
    )
    assert shell.entries() == [
        ("▶ agent-1 (coder) started", "agent"),
        ("orbit → agent-1: Do X.", "agent"),
        ("📌 1 stash", "verify"),
    ]


async def test_usage_shows_in_its_panel_and_nothing_in_the_conversation() -> None:
    shell = Shell()
    usage = UsageChanged(status="unavailable", detail="not logged in")
    await shell.feed(usage, Notice(message="1 stash"))
    assert shell.entries() == [("📌 1 stash", "verify")]
    assert shell.usage.reports == [usage]


async def test_a_prompt_stops_the_typing_and_the_planner_going_free_doesnt_start_it() -> None:
    shell = Shell()
    await shell.submit("hi")
    assert not shell.prompt.typing
    await shell.feed(BusyChanged(busy=True, lane="planner"))
    await shell.feed(BusyChanged(busy=False, lane="planner"))
    assert not shell.prompt.typing
    shell.frontend.insert_mode()
    assert shell.prompt.typing


def task(task_id: str, role: str = "coder") -> TaskState:
    return TaskState(id=task_id, task=TaskSpec(prompt=f"do {task_id}", role=role))


BURST: list[Event] = [
    TasksChanged(tasks=(task("1"),)),
    TasksChanged(tasks=(task("1"), task("2", "reviewer"))),
    VerificationChanged(steps=(StepState(name="Lint"),)),
    TaskStarted(task_id="2"),
    AgentStarted(agent_id="agent-1", role="reviewer", task_id="1"),
    AssistantMessage(turn_id="t", text="looking", agent_id="agent-1"),
    TasksChanged(tasks=(task("2", "reviewer"),)),
    TasksChanged(tasks=(task("2", "reviewer"), task("3"))),
    TaskStarted(task_id="2"),
    VerificationStarted(task_id="2", step=0),
    VerificationFinished(task_id="2", step=0, passed=False),
    VerificationFinished(task_id="2", step=0, passed=True),
    VerificationSkipped(task_id="2", reason="no_changes"),
    TaskMerged(task_id="2", result="merged"),
]
"""Events that come in a burst, two of whose task lists a later one supersedes."""


def visible_state(shell: Shell) -> object:
    return (
        shell.entries(),
        shell.tab_strip,
        shell.tasks.tasks,
        shell.tasks.numbers,
        shell.agents.rows,
        shell.steps.steps,
    )


async def test_a_burst_shows_its_events_but_those_superseded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell = Shell()
    rendered: list[Event] = []
    monkeypatch.setattr(shell.frontend, "render", rendered.append)
    shell.frontend.show_batch(BURST)
    assert rendered == coalesce(BURST)
    assert len(rendered) == len(BURST) - 2


async def test_a_burst_ends_where_showing_each_event_alone_would() -> None:
    one_by_one = Shell()
    for event in BURST:
        await one_by_one.feed(event)
    burst = Shell()
    await burst.feed(*BURST)
    assert visible_state(burst) == visible_state(one_by_one)
