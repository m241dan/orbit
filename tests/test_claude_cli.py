import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from orbit.agent import AgentSession, ClaudeCliRunner
from orbit.agent.claude_cli import translate
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
