import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from orbit.agent import ClaudeCliRunner
from orbit.agent.application.ports import AgentLaunch, ResumeFailed
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.roles import ORBIT_TOOLS
from orbit.agent.domain.usage_limits import limit_hit
from orbit.agent.external.claude_cli import (
    AGENT_RULES,
    TOOL_TIMEOUT_MS,
    agent_telemetry_env,
    translate,
)
from orbit.protocol import (
    AGENT_FAILED,
    PLAN_TASK,
    AssistantMessage,
    Effort,
    ErrorOccurred,
    Event,
    Settings,
    ToolAccess,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    Usage,
)
from tests.process_checks import gone
from tests.waiting import eventually

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"
SESSION_ID = "49782958-d074-475d-9aaa-54d462f567dc"

# Shapes captured from a real `claude -p --output-format stream-json --verbose` run.
INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": SESSION_ID,
    "model": "x",
    "mcp_servers": [{"name": "orbit", "status": "connected", "source": "dynamic"}],
}
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
                ErrorOccurred(turn_id="t", code=AGENT_FAILED, message="Bad key"),
                TurnFinished(turn_id="t", reason="error"),
            ],
        ),
        (
            {"type": "result", "subtype": "error_during_execution", "is_error": True},
            [
                ErrorOccurred(turn_id="t", code=AGENT_FAILED, message="error_during_execution"),
                TurnFinished(turn_id="t", reason="error"),
            ],
        ),
    ],
)
def test_translate_result(result: dict[str, Any], expected: list[Event]) -> None:
    assert translate("t", result) == expected


def test_translate_result_carries_the_turns_usage_and_cost() -> None:
    result = {
        **RESULT,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 40,
            "server_tool_use": {"web_search_requests": 0},
        },
        "total_cost_usd": 0.84,
        "num_turns": 3,
        "duration_ms": 1500,
    }
    usage = Usage(
        input_tokens=10,
        output_tokens=20,
        cache_creation_input_tokens=30,
        cache_read_input_tokens=40,
        cost_usd=0.84,
        turns=3,
        duration_ms=1500,
    )
    assert translate("t", result) == [TurnFinished(turn_id="t", reason="completed", usage=usage)]
    failed = {"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 9}
    assert translate("t", failed) == [
        TurnFinished(turn_id="t", reason="max_steps", usage=Usage(turns=9))
    ]
    errored = {"type": "result", "subtype": "x", "is_error": True, "total_cost_usd": 0.5}
    assert translate("t", errored)[-1] == TurnFinished(
        turn_id="t", reason="error", usage=Usage(cost_usd=0.5)
    )


@pytest.mark.parametrize("message", [INIT, RATE_LIMIT, {"type": "something_new"}])
def test_translate_ignores_other_messages(message: dict[str, Any]) -> None:
    assert translate("t", message) == []


@pytest.mark.parametrize(
    "message",
    [
        {"type": "assistant", "message": {}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_1"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        {"type": "user", "message": {"content": None}},
        {"type": "result", "is_error": True},
    ],
)
def test_translate_rejects_messages_missing_what_they_should_hold(
    message: dict[str, Any],
) -> None:
    with pytest.raises(RuntimeError, match=r"^Unexpected output from claude: \{") as raised:
        translate("t", message)
    assert json.dumps(message)[:40] in str(raised.value)


# ClaudeCliRunner: drives a real subprocess (the fake CLI above)


def make_runner(
    tmp_path: Path, endpoint: str | None = None, **scenario: Any
) -> tuple[ClaudeCliRunner, Path]:
    """A runner of the fake CLI playing ``scenario``, its agents sending their telemetry to the
    collector at ``endpoint`` over OTLP/gRPC, if given, and where its calls are recorded."""
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    command = (sys.executable, str(FAKE_CLAUDE), str(scenario_path))
    runner = ClaudeCliRunner(command=command, endpoint=endpoint, protocol="grpc")
    return runner, scenario_path


def calls(scenario_path: Path) -> list[dict[str, Any]]:
    lines = Path(f"{scenario_path}.calls").read_text().splitlines()
    return [json.loads(line) for line in lines]


def lines(*messages: dict[str, Any]) -> list[str]:
    return [json.dumps(m) for m in messages]


# The agent the runner's turns below are a conversation with.
AGENT = Agent(id="agent-3", role="coder", task_id="7", token="s3cret")
URL = "http://127.0.0.1:5555/mcp"
WORKSPACE_ID = "workspace-1"
STREAM = ["-p", "--output-format", "stream-json", "--verbose"]


async def run(
    runner: ClaudeCliRunner, prompt: str, settings: Settings | None = None
) -> list[Event]:
    """One turn, ``t``, of AGENT's conversation through ``runner``."""
    launch = AgentLaunch(
        AGENT, URL, prompt, settings or Settings(), ".", "Plan.", ("x",), WORKSPACE_ID
    )
    return [e async for e in runner.converse("t", launch)]


def own_args(call: dict[str, Any]) -> list[str]:
    """The flags a call was given before those connecting it to orbit."""
    argv: list[str] = call["argv"]
    return argv[: argv.index("--mcp-config")]


async def test_runner_streams_a_turn(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(*TRANSCRIPT))
    events = await run(runner, "--what version?")

    assert events == [
        TurnStarted(turn_id="t", agent_id="agent-3"),
        ToolCallStarted(
            turn_id="t",
            agent_id="agent-3",
            call_id="toolu_1",
            name="Read",
            arguments={"file_path": "/repo/.python-version"},
        ),
        ToolCallFinished(turn_id="t", agent_id="agent-3", call_id="toolu_1", output="1\t3.13\n"),
        AssistantMessage(turn_id="t", agent_id="agent-3", text="3.13"),
        TurnFinished(turn_id="t", agent_id="agent-3", reason="completed"),
    ]
    [call] = calls(scenario)
    assert own_args(call) == STREAM
    assert call["stdin"] == "--what version?"


@pytest.mark.parametrize(
    ("settings", "extra_args"),
    [
        (Settings(), []),
        (Settings(model="opus"), ["--model", "opus"]),
        (Settings(tool_access="off"), ["--tools", ""]),
        (Settings(tool_access="full"), ["--permission-mode", "bypassPermissions"]),
        (Settings(effort="high"), ["--effort", "high"]),
        (
            Settings(model="claude-sonnet-5-5", tool_access="full"),
            ["--model", "claude-sonnet-5-5", "--permission-mode", "bypassPermissions"],
        ),
        (
            Settings(model="opus", tool_access="off", effort="max"),
            ["--model", "opus", "--effort", "max", "--tools", ""],
        ),
    ],
)
async def test_runner_passes_settings_as_flags(
    tmp_path: Path, settings: Settings, extra_args: list[str]
) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(RESULT))
    await run(runner, "hi", settings)
    [call] = calls(scenario)
    assert own_args(call) == [*STREAM, *extra_args]


async def test_runner_resumes_session_on_next_turn(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(*TRANSCRIPT))
    await run(runner, "first")
    await run(runner, "second", Settings(model="opus"))

    first, second = calls(scenario)
    assert "--resume" not in first["argv"]
    assert own_args(second)[-4:] == ["--model", "opus", "--resume", SESSION_ID]


async def test_runner_does_not_resume_without_session_id(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines({"type": "result", "subtype": "success"}))
    await run(runner, "first")
    await run(runner, "second")

    assert all("--resume" not in call["argv"] for call in calls(scenario))


async def test_a_conversation_set_for_an_agent_is_resumed_on_its_first_turn(
    tmp_path: Path,
) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(*TRANSCRIPT))
    runner.set_conversation("agent-3", "saved-7")
    assert runner.conversation("agent-3") == "saved-7"
    await run(runner, "Carry on.")
    [call] = calls(scenario)
    assert own_args(call)[-2:] == ["--resume", "saved-7"]
    assert runner.conversation("agent-3") == SESSION_ID  # What the CLI reported.


async def test_the_conversation_is_known_after_a_turn_and_can_be_dropped(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(*TRANSCRIPT))
    assert runner.conversation("agent-3") is None
    await run(runner, "first")
    assert runner.conversation("agent-3") == SESSION_ID
    runner.set_conversation("agent-3", None)
    runner.set_conversation("agent-3", None)  # Dropping none is fine too.
    await run(runner, "second")
    assert all("--resume" not in call["argv"] for call in calls(scenario))


async def test_a_resumed_turn_failing_before_the_cli_reports_its_session_fails_to_resume(
    tmp_path: Path,
) -> None:
    stderr = "No conversation found with session ID: saved-7\n"
    runner, _ = make_runner(tmp_path, stderr=stderr, exit=1)
    runner.set_conversation("agent-3", "saved-7")
    with pytest.raises(ResumeFailed, match=r"^No conversation found with session ID: saved-7$"):
        await run(runner, "Carry on.")


async def test_a_resumed_turn_failing_after_the_cli_reports_its_session_just_fails(
    tmp_path: Path,
) -> None:
    runner, _ = make_runner(tmp_path, stdout=lines(INIT), stderr="Invalid API key\n", exit=1)
    runner.set_conversation("agent-3", "saved-7")
    with pytest.raises(RuntimeError, match=r"^Invalid API key$") as raised:
        await run(runner, "Carry on.")
    assert not isinstance(raised.value, ResumeFailed)


async def test_a_new_turn_failing_is_not_a_failure_to_resume(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, exit=3)
    with pytest.raises(RuntimeError) as raised:
        await run(runner, "hi")
    assert not isinstance(raised.value, ResumeFailed)


async def test_a_resumed_turn_whose_cli_cannot_start_is_not_a_failure_to_resume(
    tmp_path: Path,
) -> None:
    runner = ClaudeCliRunner(command=(str(tmp_path / "no-such-claude"),))
    runner.set_conversation("agent-3", "saved-7")
    with pytest.raises(RuntimeError, match=r"Could not start .*no-such-claude") as raised:
        await run(runner, "Carry on.")
    assert not isinstance(raised.value, ResumeFailed)
    assert runner.conversation("agent-3") == "saved-7"


async def test_a_resumed_turn_with_unexpected_output_is_not_a_failure_to_resume(
    tmp_path: Path,
) -> None:
    runner, _ = make_runner(tmp_path, stdout=["Error: something odd"])
    runner.set_conversation("agent-3", "saved-7")
    with pytest.raises(RuntimeError, match="Unexpected output from claude") as raised:
        await run(runner, "Carry on.")
    assert not isinstance(raised.value, ResumeFailed)


# Larger than a pipe's buffer, so sending it to a CLI that has exited breaks the pipe.
LARGE_PROMPT = "x" * (4 * 1024 * 1024)


async def test_an_agent_whose_cli_exits_before_reading_its_prompt_crashes_with_its_stderr(
    tmp_path: Path,
) -> None:
    runner, _ = make_runner(tmp_path, exit_at_once=True, stderr="error: unknown option\n", exit=1)
    launch = AgentLaunch(
        AGENT, URL, LARGE_PROMPT, Settings(), ".", "Write code.", TOOLS, WORKSPACE_ID
    )
    with pytest.raises(RuntimeError, match=r"^error: unknown option$"):
        [e async for e in runner.run_agent(launch)]


async def test_a_resumed_turn_whose_cli_exits_before_reading_its_prompt_fails_to_resume(
    tmp_path: Path,
) -> None:
    stderr = "No conversation found with session ID: saved-7\n"
    runner, _ = make_runner(tmp_path, exit_at_once=True, stderr=stderr, exit=1)
    runner.set_conversation("agent-3", "saved-7")
    with pytest.raises(ResumeFailed, match=r"^No conversation found with session ID: saved-7$"):
        await run(runner, LARGE_PROMPT)


async def test_runner_skips_blank_lines(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, stdout=["", *lines(TEXT), "  ", *lines(RESULT)])
    events = await run(runner, "hi")
    assert events[-1] == TurnFinished(turn_id="t", agent_id="agent-3", reason="completed")


async def test_runner_reports_crash_with_stderr(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, stdout=lines(INIT), stderr="Invalid API key\n", exit=1)
    with pytest.raises(RuntimeError, match=r"^Invalid API key$"):
        await run(runner, "hi")


async def test_runner_reports_crash_without_stderr(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, exit=3)
    with pytest.raises(RuntimeError, match="claude exited with status 3"):
        await run(runner, "hi")


# Lone surrogates the fake CLI writes as the bytes 0xff and 0xfe: invalid UTF-8.
INVALID_UTF8 = "\udcff\udcfe"


async def test_a_crash_whose_stderr_is_not_utf8_reports_it_with_replacements(
    tmp_path: Path,
) -> None:
    runner, _ = make_runner(tmp_path, stderr=f"Bad {INVALID_UTF8} key\n", exit=1)
    with pytest.raises(RuntimeError, match=r"^Bad \ufffd\ufffd key$"):
        await run(runner, "hi")


async def test_a_resumed_turn_whose_stderr_is_not_utf8_fails_to_resume(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, stderr=f"No conversation {INVALID_UTF8}\n", exit=1)
    runner.set_conversation("agent-3", "saved-7")
    with pytest.raises(ResumeFailed, match=r"^No conversation \ufffd\ufffd$"):
        await run(runner, "Carry on.")


async def test_a_resumed_turn_whose_cli_exits_at_once_with_non_utf8_stderr_fails_to_resume(
    tmp_path: Path,
) -> None:
    stderr = f"No conversation {INVALID_UTF8}\n"
    runner, _ = make_runner(tmp_path, exit_at_once=True, stderr=stderr, exit=1)
    runner.set_conversation("agent-3", "saved-7")
    with pytest.raises(ResumeFailed, match=r"^No conversation \ufffd\ufffd$"):
        await run(runner, "Carry on.")


async def test_a_crash_with_invalid_bytes_around_a_usage_limit_still_says_so(
    tmp_path: Path,
) -> None:
    stderr = f"{INVALID_UTF8}You've hit your session limit{INVALID_UTF8}\n"
    runner, _ = make_runner(tmp_path, stdout=lines(INIT), stderr=stderr, exit=1)
    with pytest.raises(RuntimeError) as raised:
        await run(runner, "hi")
    assert "You've hit your session limit" in str(raised.value)
    assert limit_hit(str(raised.value))


async def test_a_stream_message_missing_its_content_fails_as_unexpected_output(
    tmp_path: Path,
) -> None:
    runner, _ = make_runner(tmp_path, stdout=lines(INIT, {"type": "assistant", "message": {}}))
    with pytest.raises(RuntimeError, match=r"^Unexpected output from claude: .*assistant"):
        await run(runner, "hi")


async def test_a_resumed_turn_whose_output_is_not_utf8_still_fails_to_resume(
    tmp_path: Path,
) -> None:
    text = {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}
    line = json.dumps(text).replace('"x"', f'"{INVALID_UTF8}"')
    runner, _ = make_runner(tmp_path, stdout=[line], exit=1)
    runner.set_conversation("agent-3", "saved-7")
    with pytest.raises(ResumeFailed, match=r"^claude exited with status 1$"):
        await run(runner, "Carry on.")


async def test_runner_rejects_non_json_output(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, stdout=["Error: something odd"])
    with pytest.raises(RuntimeError, match="Unexpected output from claude: Error: something odd"):
        await run(runner, "hi")


async def test_runner_reports_missing_cli(tmp_path: Path) -> None:
    runner = ClaudeCliRunner(command=(str(tmp_path / "no-such-claude"),))
    with pytest.raises(RuntimeError) as raised:
        await run(runner, "hi")
    assert str(raised.value) == (
        f"Could not start {str(tmp_path / 'no-such-claude')!r}: no such program. "
        "Is the Claude CLI installed and on PATH?"
    )


async def test_runner_reports_a_missing_directory_not_a_missing_cli(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path)
    missing = tmp_path / "gone"
    launch = AgentLaunch(AGENT, URL, "hi", Settings(), str(missing), "Plan.", ("x",), WORKSPACE_ID)
    with pytest.raises(RuntimeError) as raised:
        [e async for e in runner.converse("t", launch)]
    assert str(raised.value) == (
        f"Could not start {sys.executable!r}: there's no directory {missing} to run it in."
    )


async def test_runner_reports_a_cli_it_may_not_run(tmp_path: Path) -> None:
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o644)
    runner = ClaudeCliRunner(command=(str(script),))
    with pytest.raises(RuntimeError) as raised:
        await run(runner, "hi")
    assert str(raised.value) == f"Could not start {str(script)!r} in .: Permission denied."


async def test_cancel_kills_the_cli_process(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(INIT), hang=True)
    turn = asyncio.create_task(run(runner, "hi"))
    recorded = Path(f"{scenario}.calls")
    # The file opens before its line is written, so wait for the whole line.
    await eventually(lambda: recorded.exists() and recorded.read_text().endswith("\n"))
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    [call] = calls(scenario)
    with pytest.raises(ProcessLookupError):
        os.kill(call["pid"], 0)


async def test_cancel_stops_what_the_cli_started(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(INIT), hang=True, spawn=True)
    turn = asyncio.create_task(run(runner, "hi"))
    recorded = Path(f"{scenario}.calls")
    await eventually(lambda: recorded.exists() and recorded.read_text().endswith("\n"))
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    [call] = calls(scenario)
    assert await gone(call["child"])


async def test_cancel_stops_what_the_cli_left_holding_its_output(tmp_path: Path) -> None:
    # The CLI exits at once, but its child keeps stdout open, so the turn never sees its end.
    runner, scenario = make_runner(tmp_path, stdout=lines(INIT), spawn=True)
    turn = asyncio.create_task(run(runner, "hi"))
    recorded = Path(f"{scenario}.calls")
    await eventually(lambda: recorded.exists() and recorded.read_text().endswith("\n"))
    [call] = calls(scenario)
    assert await gone(call["pid"])
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, timeout=5)
    assert await gone(call["child"])


# Agents: a CLI connected to orbit's MCP server

TOOLS = ("get_task", "complete_task")


def init(status: str | None) -> dict[str, Any]:
    servers = [] if status is None else [{"name": "orbit", "status": status, "source": "dynamic"}]
    return {**INIT, "mcp_servers": servers}


async def run_agent(
    runner: ClaudeCliRunner, settings: Settings | None = None, cwd: str = "."
) -> list[Event]:
    launch = AgentLaunch(
        AGENT,
        URL,
        "You are agent-3.",
        settings or Settings(),
        cwd,
        "Write code.",
        TOOLS,
        WORKSPACE_ID,
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
    allowed = "mcp__orbit__get_task,mcp__orbit__complete_task,Read,Glob,Grep,WebSearch"
    assert argv[argv.index("--allowedTools") + 1] == allowed
    assert argv[argv.index("--append-system-prompt") + 1] == f"{AGENT_RULES}\n\nWrite code."
    assert "--resume" not in argv


async def test_the_token_travels_in_the_environment_not_the_arguments(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))
    await run_agent(runner)
    [call] = calls(scenario)
    assert not any("s3cret" in arg for arg in call["argv"])
    assert call["env"]["ORBIT_AGENT_TOKEN"] == "s3cret"
    assert call["env"]["ENABLE_TOOL_SEARCH"] == "false"
    assert call["env"]["MCP_TOOL_TIMEOUT"] == str(TOOL_TIMEOUT_MS)


async def test_agents_start_without_the_users_auto_memory(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))
    await run_agent(runner)
    [call] = calls(scenario)
    assert call["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


async def test_restricted_agents_can_read_and_search_but_not_run_or_write(tmp_path: Path) -> None:
    (tmp_path / "tree").mkdir()
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))
    await run_agent(runner, Settings(tool_access="restricted"), cwd=str(tmp_path / "tree"))
    [call] = calls(scenario)
    argv = call["argv"]
    assert "--restricted" in argv
    assert argv[argv.index("--allowedTools") + 1].endswith(",Read,Glob,Grep,WebSearch")
    assert argv[argv.index("--disallowedTools") + 1].endswith(",Edit,Write,NotebookEdit")
    # --restricted skips CLAUDE.md discovery; naming the tree brings its CLAUDE.md back.
    assert argv[argv.index("--add-dir") + 1] == str(tmp_path / "tree")
    assert call["env"]["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] == "1"


async def test_restricted_agents_name_their_tree_by_its_full_path(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))
    await run_agent(runner, cwd=".")
    argv = calls(scenario)[0]["argv"]
    assert argv[argv.index("--add-dir") + 1] == os.path.abspath(".")


@pytest.mark.parametrize("access", ["off", "full"])
async def test_only_restricted_agents_get_the_read_only_flags(
    tmp_path: Path, access: ToolAccess
) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))
    await run_agent(runner, Settings(tool_access=access))
    argv = calls(scenario)[0]["argv"]
    assert not {"--restricted", "--add-dir"} & set(argv)
    assert "Read" not in argv[argv.index("--allowedTools") + 1]
    assert "WebSearch" not in argv[argv.index("--allowedTools") + 1]
    assert "Edit" not in argv[argv.index("--disallowedTools") + 1]


async def test_orbit_tools_the_role_lacks_are_hidden_from_the_agent(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))
    await run_agent(runner, Settings(tool_access="full"))
    argv = calls(scenario)[0]["argv"]
    hidden = argv[argv.index("--disallowedTools") + 1].split(",")
    assert hidden == [f"mcp__orbit__{tool}" for tool in ORBIT_TOOLS if tool not in TOOLS]
    assert not set(hidden) & set(argv[argv.index("--allowedTools") + 1].split(","))


async def test_an_agent_with_every_orbit_tool_has_none_hidden(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))
    settings = Settings(tool_access="full")
    launch = AgentLaunch(AGENT, URL, "Go.", settings, ".", "Code.", ORBIT_TOOLS, WORKSPACE_ID)
    async for _ in runner.run_agent(launch):
        pass
    assert "--disallowedTools" not in calls(scenario)[0]["argv"]


async def test_agents_keep_tool_access_flags_and_orbit_tools(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))
    await run_agent(runner, Settings(tool_access="off"))
    argv = calls(scenario)[0]["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--allowedTools") + 1].startswith("mcp__orbit__get_task")


async def test_an_agents_effort_adds_only_its_flag(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))
    await run_agent(runner, Settings(model="opus", tool_access="full"))
    await run_agent(runner, Settings(model="opus", tool_access="full", effort="max"))
    plain, effortful = calls(scenario)
    after_model = plain["argv"].index("opus") + 1
    flagged = [*plain["argv"][:after_model], "--effort", "max", *plain["argv"][after_model:]]
    assert (effortful["argv"], effortful["env"]) == (flagged, plain["env"])


@pytest.mark.parametrize("effort", [None, "high"])
@pytest.mark.parametrize("endpoint", [None, "http://c:4317"])
async def test_agents_run_without_the_effort_level_in_orbits_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effort: Effort | None, endpoint: str | None
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    stdout = lines(init("connected"), RESULT)
    runner, scenario = make_runner(tmp_path, endpoint=endpoint, stdout=stdout)

    await run_agent(runner, Settings(effort=effort))
    await run(runner, "hi", Settings(effort=effort))

    assert all("CLAUDE_CODE_EFFORT_LEVEL" not in call["env"] for call in calls(scenario))


async def test_each_agent_run_is_a_fresh_session(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))
    await run_agent(runner)
    await run_agent(runner)
    await run(runner, "hi")  # Nor does it resume an agent run's session.
    assert all("--resume" not in call["argv"] for call in calls(scenario))


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
    launch = AgentLaunch(
        agent, URL, prompt, Settings(), ".", "Plan.", ("propose_task",), WORKSPACE_ID
    )
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
    allowed = "mcp__orbit__propose_task,Read,Glob,Grep,WebSearch"  # The planner is restricted.
    assert two["argv"][two["argv"].index("--allowedTools") + 1] == allowed
    assert two["env"]["ORBIT_AGENT_TOKEN"] == "s3cret"


# Agents' own telemetry, sent to the collector orbit's goes to

RESOURCE = {
    "orbit.task.id": "t1",
    "orbit.agent.id": "agent-1",
    "orbit.role": "coder",
    "orbit.workspace.id": "w",
}
TELEMETRY_VARIABLES = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_RESOURCE_ATTRIBUTES",
)
HEADER_VARIABLES = (
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
)


@pytest.fixture
def untelemetered(monkeypatch: pytest.MonkeyPatch) -> None:
    """An environment for orbit setting none of the variables agents' telemetry takes."""
    for name in (*TELEMETRY_VARIABLES, *HEADER_VARIABLES):
        monkeypatch.delenv(name, raising=False)


def test_agents_send_their_metrics_and_logs_to_the_collector_describing_themselves() -> None:
    assert agent_telemetry_env("http://c:4318", "http/protobuf", RESOURCE) == {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318",
        "OTEL_RESOURCE_ATTRIBUTES": (
            "orbit.task.id=t1,orbit.agent.id=agent-1,orbit.role=coder,orbit.workspace.id=w"
        ),
    }


def test_the_resource_attributes_values_are_percent_encoded() -> None:
    env = agent_telemetry_env("http://c:4317", "grpc", {"orbit.task.id": "a b,c=d%é"})

    assert env["OTEL_RESOURCE_ATTRIBUTES"] == "orbit.task.id=a%20b%2Cc%3Dd%25%C3%A9"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc"


def test_without_a_collector_agents_send_no_telemetry() -> None:
    assert agent_telemetry_env(None, "http/protobuf", RESOURCE) == {}


@pytest.mark.usefixtures("untelemetered")
async def test_an_agent_sends_its_telemetry_to_the_collector_without_orbits_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in HEADER_VARIABLES:
        monkeypatch.setenv(name, "Authorization=Bearer%20orbits-key")
    stdout = lines(init("connected"), RESULT)
    runner, scenario = make_runner(tmp_path, endpoint="http://c:4317", stdout=stdout)

    await run_agent(runner)

    env = calls(scenario)[0]["env"]
    assert {name: env.get(name) for name in TELEMETRY_VARIABLES} == {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4317",
        "OTEL_RESOURCE_ATTRIBUTES": (
            "orbit.task.id=7,orbit.agent.id=agent-3,orbit.role=coder,orbit.workspace.id=workspace-1"
        ),
    }
    assert not set(HEADER_VARIABLES) & set(env)
    assert env["ORBIT_AGENT_TOKEN"] == "s3cret"


@pytest.mark.usefixtures("untelemetered")
async def test_the_planners_telemetry_names_no_task(tmp_path: Path) -> None:
    stdout = lines(init("connected"), RESULT)
    runner, scenario = make_runner(tmp_path, endpoint="http://c:4317", stdout=stdout)
    planner = Agent(id="agent-9", role="planner", task_id=PLAN_TASK, token="t9")

    await converse(runner, "Hello.", planner)

    attributes = calls(scenario)[0]["env"]["OTEL_RESOURCE_ATTRIBUTES"]
    assert attributes == "orbit.agent.id=agent-9,orbit.role=planner,orbit.workspace.id=workspace-1"


@pytest.mark.usefixtures("untelemetered")
async def test_without_a_collector_an_agent_sends_no_telemetry_and_keeps_its_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-key=yours")
    runner, scenario = make_runner(tmp_path, stdout=lines(init("connected"), RESULT))

    await run_agent(runner)

    env = calls(scenario)[0]["env"]
    assert not set(TELEMETRY_VARIABLES) & set(env)
    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == "x-key=yours"
