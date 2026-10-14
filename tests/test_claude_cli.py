import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from orbit.agent import AgentSession, ClaudeCliRunner
from orbit.agent.application.ports import AgentLaunch
from orbit.agent.domain.agents import Agent
from orbit.agent.external.claude_cli import AGENT_RULES, TOOL_TIMEOUT_MS, translate
from orbit.protocol import (
    AssistantMessage,
    BusyChanged,
    CancelTurn,
    ErrorOccurred,
    Event,
    Settings,
    SettingsChanged,
    SubmitPrompt,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"
SESSION_ID = "49782958-d074-475d-9aaa-54d462f567dc"

# Shapes captured from a real `claude -p --output-format stream-json --verbose` run.
INIT = {"type": "system", "subtype": "init", "session_id": SESSION_ID, "model": "x"}
TOOL_USE = {
    "type": "assistant",
    "message": {
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "Read",
                "input": {"file_path": "/repo/.python-version"},
            }
        ]
    },
    "session_id": SESSION_ID,
}
RATE_LIMIT = {"type": "rate_limit_event", "session_id": SESSION_ID}
TOOL_RESULT = {
    "type": "user",
    "message": {
        "content": [{"tool_use_id": "toolu_1", "type": "tool_result", "content": "1\t3.13\n"}]
    },
    "session_id": SESSION_ID,
}
TEXT = {
    "type": "assistant",
    "message": {"content": [{"type": "text", "text": "3.13"}]},
    "session_id": SESSION_ID,
}
RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "3.13",
    "session_id": SESSION_ID,
}
TRANSCRIPT: list[dict[str, Any]] = [INIT, TOOL_USE, RATE_LIMIT, TOOL_RESULT, TEXT, RESULT]


# translate(): one stream-json message -> protocol events


def test_translate_assistant_text_and_tool_use() -> None:
    message = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "Let me look."},
                {"type": "text", "text": ""},
                {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"a": 1}},
            ]
        },
    }
    assert translate("t", message) == [
        AssistantMessage(turn_id="t", text="Let me look."),
        ToolCallStarted(turn_id="t", call_id="toolu_1", name="Read", arguments={"a": 1}),
    ]


def test_translate_tool_results() -> None:
    message = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "a", "content": "plain"},
                {
                    "type": "tool_result",
                    "tool_use_id": "b",
                    "content": [
                        {"type": "text", "text": "line 1"},
                        {"type": "image", "source": {}},
                        {"type": "text", "text": "line 2"},
                    ],
                    "is_error": True,
                },
                {"type": "text", "text": "not a tool result"},
            ]
        },
    }
    assert translate("t", message) == [
        ToolCallFinished(turn_id="t", call_id="a", output="plain"),
        ToolCallFinished(turn_id="t", call_id="b", output="line 1\nline 2", is_error=True),
    ]


def test_translate_ignores_plain_user_text() -> None:
    assert translate("t", {"type": "user", "message": {"content": "replayed prompt"}}) == []


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (RESULT, [TurnFinished(turn_id="t", reason="completed")]),
        (
            {"type": "result", "subtype": "error_max_turns", "is_error": True},
            [TurnFinished(turn_id="t", reason="max_steps")],
        ),
        (
            {"type": "result", "subtype": "success", "is_error": True, "result": "Bad key"},
            [
                ErrorOccurred(turn_id="t", message="Bad key"),
                TurnFinished(turn_id="t", reason="error"),
            ],
        ),
        (
            {"type": "result", "subtype": "error_during_execution", "is_error": True},
            [
                ErrorOccurred(turn_id="t", message="error_during_execution"),
                TurnFinished(turn_id="t", reason="error"),
            ],
        ),
    ],
)
def test_translate_result(result: dict[str, Any], expected: list[Event]) -> None:
    assert translate("t", result) == expected


@pytest.mark.parametrize("message", [INIT, RATE_LIMIT, {"type": "something_new"}])
def test_translate_ignores_other_messages(message: dict[str, Any]) -> None:
    assert translate("t", message) == []


# ClaudeCliRunner: drives a real subprocess (the fake CLI above)


def make_runner(tmp_path: Path, **scenario: Any) -> tuple[ClaudeCliRunner, Path]:
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    runner = ClaudeCliRunner(command=(sys.executable, str(FAKE_CLAUDE), str(scenario_path)))
    return runner, scenario_path


def calls(scenario_path: Path) -> list[dict[str, Any]]:
    lines = Path(f"{scenario_path}.calls").read_text().splitlines()
    return [json.loads(line) for line in lines]


def lines(*messages: dict[str, Any]) -> list[str]:
    return [json.dumps(m) for m in messages]


async def run(
    runner: ClaudeCliRunner, prompt: str, settings: Settings | None = None
) -> list[Event]:
    return [e async for e in runner.run_turn("t", prompt, settings or Settings())]


async def test_runner_streams_a_turn(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(*TRANSCRIPT))
    events = await run(runner, "--what version?")

    assert events == [
        TurnStarted(turn_id="t"),
        ToolCallStarted(
            turn_id="t",
            call_id="toolu_1",
            name="Read",
            arguments={"file_path": "/repo/.python-version"},
        ),
        ToolCallFinished(turn_id="t", call_id="toolu_1", output="1\t3.13\n"),
        AssistantMessage(turn_id="t", text="3.13"),
        TurnFinished(turn_id="t", reason="completed"),
    ]
    [call] = calls(scenario)
    assert call["argv"] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert call["stdin"] == "--what version?"


@pytest.mark.parametrize(
    ("settings", "extra_args"),
    [
        (Settings(), []),
        (Settings(model="opus"), ["--model", "opus"]),
        (Settings(tool_access="off"), ["--tools", ""]),
        (Settings(tool_access="full"), ["--permission-mode", "bypassPermissions"]),
        (
            Settings(model="claude-sonnet-5-5", tool_access="full"),
            ["--model", "claude-sonnet-5-5", "--permission-mode", "bypassPermissions"],
        ),
    ],
)
async def test_runner_passes_settings_as_flags(
    tmp_path: Path, settings: Settings, extra_args: list[str]
) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(RESULT))
    await run(runner, "hi", settings)
    [call] = calls(scenario)
    assert call["argv"] == ["-p", "--output-format", "stream-json", "--verbose", *extra_args]


async def test_runner_resumes_session_on_next_turn(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(*TRANSCRIPT))
    await run(runner, "first")
    await run(runner, "second", Settings(model="opus"))

    first, second = calls(scenario)
    assert "--resume" not in first["argv"]
    assert second["argv"][-4:] == ["--model", "opus", "--resume", SESSION_ID]


async def test_runner_does_not_resume_without_session_id(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines({"type": "result", "subtype": "success"}))
    await run(runner, "first")
    await run(runner, "second")

    assert runner.session_id is None
    assert all("--resume" not in call["argv"] for call in calls(scenario))


async def test_runner_skips_blank_lines(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, stdout=["", *lines(TEXT), "  ", *lines(RESULT)])
    events = await run(runner, "hi")
    assert events[-1] == TurnFinished(turn_id="t", reason="completed")


async def test_runner_reports_crash_with_stderr(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, stdout=lines(INIT), stderr="Invalid API key\n", exit=1)
    with pytest.raises(RuntimeError, match=r"^Invalid API key$"):
        await run(runner, "hi")


async def test_runner_reports_crash_without_stderr(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, exit=3)
    with pytest.raises(RuntimeError, match="claude exited with status 3"):
        await run(runner, "hi")


async def test_runner_rejects_non_json_output(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, stdout=["Error: something odd"])
    with pytest.raises(RuntimeError, match="Unexpected output from claude: Error: something odd"):
        await run(runner, "hi")


async def test_runner_reports_missing_cli(tmp_path: Path) -> None:
    runner = ClaudeCliRunner(command=(str(tmp_path / "no-such-claude"),))
    with pytest.raises(RuntimeError, match=r"Could not start .*no-such-claude"):
        await run(runner, "hi")


async def test_cancel_kills_the_cli_process(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(INIT), hang=True)
    session = AgentSession(runner)
    stream = session.events()
    assert isinstance(await anext(stream), SettingsChanged)

    await session.send(SubmitPrompt(text="hi"))
    assert await anext(stream) == BusyChanged(busy=True)
    assert isinstance(await anext(stream), TurnStarted)
    while not Path(f"{scenario}.calls").exists():
        await asyncio.sleep(0.01)
    await session.send(CancelTurn())

    finished = await anext(stream)
    assert isinstance(finished, TurnFinished) and finished.reason == "cancelled"
    [call] = calls(scenario)
    with pytest.raises(ProcessLookupError):
        os.kill(call["pid"], 0)


async def test_new_context_starts_a_fresh_claude_session(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(*TRANSCRIPT))
    await run(runner, "first")
    fresh = runner.new_context()
    assert fresh.session_id is None
    await run(fresh, "task")
    await run(runner, "second")

    _, task, second = calls(scenario)
    assert "--resume" not in task["argv"]
    assert task["stdin"] == "task"
    # The original keeps its own conversation.
    assert second["argv"][-2:] == ["--resume", SESSION_ID]


# Agents: a CLI connected to orbit's MCP server

AGENT = Agent(id="agent-3", role="coder", task_id="7", token="s3cret")
URL = "http://127.0.0.1:5555/mcp"
TOOLS = ("get_task", "complete_task")


def init(status: str | None) -> dict[str, Any]:
    servers = [] if status is None else [{"name": "orbit", "status": status, "source": "dynamic"}]
    return {**INIT, "mcp_servers": servers}


async def run_agent(
    runner: ClaudeCliRunner, settings: Settings | None = None, cwd: str = "."
) -> list[Event]:
    launch = AgentLaunch(
        AGENT, URL, "You are agent-3.", settings or Settings(), cwd, "Write code.", TOOLS
    )
    return [e async for e in runner.run_agent(launch)]


async def test_an_agent_runs_connected_to_orbit(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), TEXT, RESULT))
    events = await run_agent(runner, Settings(model="opus"))

    assert events == [
        TurnStarted(turn_id="agent-3", agent_id="agent-3"),
        AssistantMessage(turn_id="agent-3", text="3.13", agent_id="agent-3"),
        TurnFinished(turn_id="agent-3", reason="completed", agent_id="agent-3"),
    ]
    [call] = calls(scenario)
    assert call["stdin"] == "You are agent-3."
    argv = call["argv"]
    assert argv[:6] == ["-p", "--output-format", "stream-json", "--verbose", "--model", "opus"]
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    assert config == {
        "mcpServers": {
            "orbit": {
                "type": "http",
                "url": URL,
                "headers": {"Authorization": "Bearer ${ORBIT_AGENT_TOKEN}"},
            }
        }
    }
    assert "--strict-mcp-config" in argv
    allowed = "mcp__orbit__get_task,mcp__orbit__complete_task"
    assert argv[argv.index("--allowedTools") + 1] == allowed
    assert argv[argv.index("--append-system-prompt") + 1] == f"{AGENT_RULES}\n\nWrite code."
    assert "--resume" not in argv


async def test_the_token_travels_in_the_environment_not_the_arguments(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))
    await run_agent(runner)
    [call] = calls(scenario)
    assert not any("s3cret" in arg for arg in call["argv"])
    assert call["env"] == {
        "ORBIT_AGENT_TOKEN": "s3cret",
        "ENABLE_TOOL_SEARCH": "false",
        "MCP_TOOL_TIMEOUT": str(TOOL_TIMEOUT_MS),
    }


async def test_agents_keep_tool_access_flags_and_orbit_tools(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))
    await run_agent(runner, Settings(tool_access="off"))
    argv = calls(scenario)[0]["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--allowedTools") + 1].startswith("mcp__orbit__get_task")


async def test_an_agent_run_leaves_the_conversation_session_alone(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))
    await run_agent(runner)
    assert runner.session_id is None


@pytest.mark.parametrize("status", ["failed", "needs-auth", None])
async def test_an_agent_that_cannot_reach_orbit_fails_fast(
    tmp_path: Path, status: str | None
) -> None:
    runner, _ = make_runner(tmp_path, stdout=lines(init(status), TEXT), hang=True)
    with pytest.raises(RuntimeError, match="orbit's MCP server") as raised:
        await asyncio.wait_for(run_agent(runner), 5)
    assert str(status) in str(raised.value)


async def test_a_pending_connection_is_not_a_failure(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, stdout=lines(init("pending"), RESULT))
    events = await run_agent(runner)
    assert events[-1] == TurnFinished(turn_id="agent-3", reason="completed", agent_id="agent-3")


async def test_an_agent_runs_in_its_working_directory(tmp_path: Path) -> None:
    work = tmp_path / "tree"
    work.mkdir()
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))
    await run_agent(runner, cwd=str(work))
    assert calls(scenario)[0]["cwd"] == str(work)


async def converse(runner: ClaudeCliRunner, prompt: str, agent: Agent = AGENT) -> list[Event]:
    launch = AgentLaunch(agent, URL, prompt, Settings(), ".", "Plan.", ("propose_task",))
    return [e async for e in runner.converse("turn-1", launch)]


async def test_a_conversation_resumes_its_own_session_turn_after_turn(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), TEXT, RESULT))
    first = await converse(runner, "Plan a CLI.")
    await converse(runner, "Smaller steps, please.")
    other = Agent(id="agent-9", role="planner", task_id="plan", token="t9")
    await converse(runner, "Hello.", other)

    assert first[0] == TurnStarted(turn_id="turn-1", agent_id="agent-3")
    assert first[-1] == TurnFinished(turn_id="turn-1", reason="completed", agent_id="agent-3")
    one, two, three = calls(scenario)
    assert "--resume" not in one["argv"]
    assert two["argv"][two["argv"].index("--resume") + 1] == SESSION_ID
    assert "--resume" not in three["argv"]  # Another agent's conversation.
    assert two["stdin"] == "Smaller steps, please."
    assert two["argv"][two["argv"].index("--allowedTools") + 1] == "mcp__orbit__propose_task"
    assert two["env"]["ORBIT_AGENT_TOKEN"] == "s3cret"
    assert runner.session_id is None
