"""What the frontend shows of the backend's events, without Textual: the Hello it opens with,
what shows in the conversation, and what shows nothing there."""

from orbit.protocol import (
    INTERNAL,
    PLANNER,
    PROTOCOL_VERSION,
    AgentReply,
    AgentStarted,
    BusyChanged,
    ErrorOccurred,
    Hello,
    Notice,
    SendMessage,
    SessionStateChanged,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    UsageChanged,
    Welcome,
)
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
