"""Agents on Kiro: ``kiro-cli`` run with each agent's own config, its stream translated into
orbit's events, its conversations carried on, its failures told and its effort noted, driven by a
fake ``kiro-cli`` replaying the captured fixtures."""

import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest

from orbit.agent import KiroCliRunner
from orbit.agent.application.ports import AgentLaunch, ResumeFailed
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.runtimes import Failure
from orbit.agent.domain.usage_limits import KIRO_LIMIT_RETRY
from orbit.agent.external.kiro_agents import KiroAgentFiles, kiro_agent_config
from orbit.agent.external.kiro_cli import AGENT_RULES
from orbit.agent.external.kiro_stream import translate_kiro
from orbit.protocol import (
    AGENT_FAILED,
    AssistantMessage,
    Effort,
    ErrorOccurred,
    Event,
    Notice,
    Settings,
    ToolAccess,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from tests.process_checks import gone
from tests.waiting import eventually

FAKE_KIRO = Path(__file__).parent / "fake_kiro.py"
FIXTURES = Path(__file__).parent / "fixtures" / "kiro"
SESSION_ID = "00000000-0000-0000-0000-000000000001"
URL = "http://127.0.0.1:5555/mcp"
WORKSPACE_ID = "workspace-1"
TOOLS = ("get_task", "complete_task", "report_progress")
CODER = Agent(id="agent-3", role="coder", task_id="7", token="s3cret")
PLANNER = Agent(id="agent-1", role="planner", task_id="plan", token="pl4n")
CHAT = ["chat", "--output-format", "stream-json"]
PINS = ["--agent-engine", "v2", "--require-mcp-startup"]


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A home directory of the test's own, whose ``~/.kiro/agents`` the agents' configs go to."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


def make_runner(tmp_path: Path, **scenario: Any) -> tuple[KiroCliRunner, Path]:
    """A runner of the fake ``kiro-cli`` playing ``scenario``, and where its calls are
    recorded."""
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    command = (sys.executable, str(FAKE_KIRO), str(scenario_path))
    return KiroCliRunner(command=command), scenario_path


def calls(scenario_path: Path) -> list[dict[str, Any]]:
    lines = Path(f"{scenario_path}.calls").read_text().splitlines()
    return [json.loads(line) for line in lines]


def agent_name(call: dict[str, Any]) -> str:
    """The name of the agent config the call ran with, checked to be one of orbit's for
    agent-3."""
    argv: list[str] = call["argv"]
    name = argv[argv.index("--agent") + 1]
    assert re.fullmatch(r"orbit-agent-3-[0-9a-f]{16}", name)
    return name


def configs(home: Path) -> list[Path]:
    """The configs of orbit's agents left in ``home``'s ``~/.kiro/agents``."""
    return sorted((home / ".kiro" / "agents").glob("orbit-*"))


def launch_of(
    agent: Agent,
    settings: Settings | None = None,
    cwd: str = ".",
    session_id: str = "session-1",
) -> AgentLaunch:
    """How ``agent`` starts, with ``settings``, in ``cwd``, in session ``session_id``."""
    return AgentLaunch(
        agent,
        URL,
        f"You are {agent.id}.",
        settings or Settings(),
        cwd,
        "Do the work.",
        TOOLS,
        WORKSPACE_ID,
        session_id=session_id,
    )


async def run_agent(runner: KiroCliRunner, launch: AgentLaunch) -> list[Event]:
    return [event async for event in runner.run_agent(launch)]


async def converse(runner: KiroCliRunner, launch: AgentLaunch, turn_id: str = "t") -> list[Event]:
    return [event async for event in runner.converse(turn_id, launch)]


def line(kind: str, **data: Any) -> str:
    """A line of Kiro's stream: an event of type ``kind`` holding ``data``."""
    return json.dumps({"type": kind, "data": data})


def fixture_message(name: str) -> str:
    """The message of the ``runError`` in the Kiro fixture ``name``."""
    lines = (FIXTURES / name).read_text().splitlines()
    [error] = [json.loads(text) for text in lines if '"runError"' in text]
    message: str = error["data"]["message"]
    return message


# Running an agent


CODER_SETTINGS = Settings(model="claude-sonnet-4.5", effort="high", tool_access="full")


@pytest.mark.usefixtures("home")
async def test_a_coder_task_launches_with_the_engine_pinned_its_model_and_effort(
    tmp_path: Path,
) -> None:
    runner, scenario = make_runner(tmp_path, fixture="orbit-tools.jsonl")
    await run_agent(runner, launch_of(CODER, CODER_SETTINGS, str(tmp_path)))

    [call] = calls(scenario)
    assert call["argv"] == [
        *CHAT,
        "--agent",
        agent_name(call),
        *PINS,
        *("--model", "claude-sonnet-4.5"),
        *("--effort", "high"),
        "--",
        "You are agent-3.",
    ]
    assert call["cwd"] == str(tmp_path)


@pytest.mark.usefixtures("home")
async def test_a_coder_task_runs_from_the_fixture_to_completion(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, fixture="orbit-tools.jsonl")
    events = await run_agent(runner, launch_of(CODER, CODER_SETTINGS))

    lines = (FIXTURES / "orbit-tools.jsonl").read_text().splitlines()
    translated = [
        e.model_copy(update={"agent_id": "agent-3"}) for e in translate_kiro("agent-3", lines)
    ]
    ignored = Notice(
        message="kiro's model claude-sonnet-4.5 ignores effort; coder runs without it."
    )
    assert events == [translated[0], ignored, *translated[1:]]
    called = [event.name for event in events if isinstance(event, ToolCallStarted)]
    assert called == ["get_task", "report_progress", "complete_task"]
    assert events[-1] == translated[-1]
    assert isinstance(events[-1], TurnFinished) and events[-1].reason == "completed"


async def test_the_agent_runs_with_its_own_config_removed_once_it_ends(
    home: Path, tmp_path: Path
) -> None:
    runner, scenario = make_runner(tmp_path, fixture="orbit-tools.jsonl")
    await run_agent(runner, launch_of(CODER, Settings(tool_access="restricted")))

    [call] = calls(scenario)
    config = kiro_agent_config(AGENT_RULES, "Do the work.", URL, "s3cret", "restricted")
    assert call["config"] == {"name": agent_name(call), **config}
    assert configs(home) == []


async def test_runs_of_the_same_agent_id_at_once_get_configs_of_their_own(
    home: Path, tmp_path: Path
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, first_scenario = make_runner(tmp_path / "a", fixture="plain.jsonl")
    second, second_scenario = make_runner(tmp_path / "b", fixture="plain.jsonl")
    await asyncio.gather(run_agent(first, launch_of(CODER)), run_agent(second, launch_of(CODER)))

    [one], [other] = calls(first_scenario), calls(second_scenario)
    assert agent_name(one) != agent_name(other)
    assert one["config"]["name"] == agent_name(one)
    assert other["config"]["name"] == agent_name(other)
    assert configs(home) == []


@pytest.mark.usefixtures("home")
async def test_without_a_model_or_effort_kiro_picks_its_own(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, fixture="plain.jsonl")
    events = await run_agent(runner, launch_of(CODER))

    [call] = calls(scenario)
    assert call["argv"] == [*CHAT, "--agent", agent_name(call), *PINS, "--", "You are agent-3."]
    assert events == [
        TurnStarted(turn_id="agent-3", agent_id="agent-3"),
        AssistantMessage(turn_id="agent-3", agent_id="agent-3", text="ok"),
        events[-1],
    ]
    assert isinstance(events[-1], TurnFinished) and events[-1].reason == "completed"


@pytest.mark.usefixtures("home")
async def test_the_agent_runs_without_orbits_otlp_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=secret")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "authorization=secret")
    monkeypatch.setenv("ORBIT_TEST_KEPT", "1")
    runner, scenario = make_runner(tmp_path, fixture="plain.jsonl")
    await run_agent(runner, launch_of(CODER))
    assert calls(scenario)[0]["env"] == {"ORBIT_TEST_KEPT": "1"}


async def test_a_cancelled_agent_is_stopped_and_its_config_removed(
    home: Path, tmp_path: Path
) -> None:
    runner, scenario = make_runner(tmp_path, stdout=[line("runStarted")], hang=True, spawn=True)
    run = asyncio.create_task(run_agent(runner, launch_of(CODER)))
    recorded = Path(f"{scenario}.calls")
    # The file opens before its line is written, so wait for the whole line.
    await eventually(lambda: recorded.exists() and recorded.read_text().endswith("\n"))
    assert configs(home) != []
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    [call] = calls(scenario)
    assert await gone(call["pid"])
    assert await gone(call["child"])
    assert configs(home) == []


async def test_an_agent_whose_config_cant_be_written_crashes_saying_so(
    home: Path, tmp_path: Path
) -> None:
    (home / ".kiro").mkdir()
    (home / ".kiro" / "agents").write_text("not a directory")
    runner, scenario = make_runner(tmp_path, fixture="plain.jsonl")
    with pytest.raises(RuntimeError, match=r"^Couldn't write the agent's Kiro config: "):
        await run_agent(runner, launch_of(CODER))
    assert not Path(f"{scenario}.calls").exists()


async def test_a_missing_cli_is_reported_and_leaves_no_config(home: Path, tmp_path: Path) -> None:
    runner = KiroCliRunner(command=(str(tmp_path / "no-such-kiro"),))
    with pytest.raises(RuntimeError) as raised:
        await run_agent(runner, launch_of(CODER))
    assert str(raised.value) == (
        f"Could not start {str(tmp_path / 'no-such-kiro')!r}: no such program. Is the Kiro CLI"
        " (kiro-cli) installed and on PATH?"
    )
    assert configs(home) == []


# How a run that doesn't finish ends


async def test_an_unreachable_mcp_server_crashes_the_agent_naming_its_url(
    home: Path, tmp_path: Path
) -> None:
    runner, _ = make_runner(tmp_path, stderr="MCP server orbit failed to start", exit=3)
    events: list[Event] = []
    with pytest.raises(RuntimeError) as raised:
        async for event in runner.run_agent(launch_of(CODER)):
            events.append(event)
    message = str(raised.value)
    assert message == f"Kiro couldn't connect to orbit's MCP server at {URL}"
    assert events == [TurnStarted(turn_id="agent-3", agent_id="agent-3")]
    assert runner.failure(message) is None
    assert configs(home) == []


@pytest.mark.usefixtures("home")
async def test_an_exit_without_the_runs_end_is_a_crash_with_kiros_stderr(tmp_path: Path) -> None:
    started = [line("runStarted"), line("metadata", sessionId=SESSION_ID)]
    runner, _ = make_runner(tmp_path, stdout=started, stderr="panicked\n", exit=101)
    with pytest.raises(RuntimeError, match=r"^panicked$"):
        await run_agent(runner, launch_of(CODER))


@pytest.mark.usefixtures("home")
async def test_an_exit_without_the_runs_end_or_stderr_names_the_status(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, exit=1)
    with pytest.raises(RuntimeError) as raised:
        await run_agent(runner, launch_of(CODER))
    assert str(raised.value) == "kiro-cli exited with status 1"


@pytest.mark.usefixtures("home")
async def test_what_the_agent_said_before_crashing_is_given_first(tmp_path: Path) -> None:
    said = json.dumps(
        {
            "type": "sessionUpdate",
            "data": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "half"},
                }
            },
        }
    )
    runner, _ = make_runner(tmp_path, stdout=[line("runStarted"), said], exit=1)
    events: list[Event] = []
    with pytest.raises(RuntimeError):
        async for event in runner.run_agent(launch_of(CODER)):
            events.append(event)
    assert events[-1] == AssistantMessage(turn_id="agent-3", agent_id="agent-3", text="half")


@pytest.mark.usefixtures("home")
async def test_a_run_error_ends_the_turn_in_error_with_kiros_message(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path, fixture="outage.jsonl", exit=1)
    events = await run_agent(runner, launch_of(CODER))
    error = ErrorOccurred(
        turn_id="agent-3",
        agent_id="agent-3",
        code=AGENT_FAILED,
        message=fixture_message("outage.jsonl"),
    )
    assert events[-2] == error
    assert isinstance(events[-1], TurnFinished) and events[-1].reason == "error"


# Conversations


@pytest.mark.usefixtures("home")
async def test_a_planner_turn_saves_its_session_and_the_next_resumes_it(tmp_path: Path) -> None:
    runner, scenario = make_runner(tmp_path, fixture="plain.jsonl")
    launch = launch_of(PLANNER, Settings(model="auto"))
    events = await converse(runner, launch)
    assert events[0] == TurnStarted(turn_id="t", agent_id="agent-1")
    assert runner.conversation("agent-1") == SESSION_ID
    await converse(runner, launch, "t2")

    first, second = calls(scenario)
    assert "--resume-id" not in first["argv"]
    assert second["argv"][-6:] == [
        *("--model", "auto"),
        *("--resume-id", SESSION_ID),
        "--",
        "You are agent-1.",
    ]


@pytest.mark.usefixtures("home")
async def test_a_conversation_set_for_an_agent_is_resumed_and_can_be_dropped(
    tmp_path: Path,
) -> None:
    runner, scenario = make_runner(tmp_path, fixture="plain.jsonl")
    runner.set_conversation("agent-1", "saved-session")
    assert runner.conversation("agent-1") == "saved-session"
    await converse(runner, launch_of(PLANNER))
    runner.set_conversation("agent-1", None)
    assert runner.conversation("agent-1") is None
    await converse(runner, launch_of(PLANNER))

    first, second = calls(scenario)
    assert first["argv"][first["argv"].index("--resume-id") + 1] == "saved-session"
    assert "--resume-id" not in second["argv"]


@pytest.mark.usefixtures("home")
async def test_a_turn_reporting_no_session_leaves_the_conversation_as_it_was(
    tmp_path: Path,
) -> None:
    runner, _ = make_runner(tmp_path, stdout=[line("runStarted"), line("runFinished")])
    runner.set_conversation("agent-1", "saved-session")
    await converse(runner, launch_of(PLANNER))
    assert runner.conversation("agent-1") == "saved-session"


async def test_a_resumed_turn_failing_to_start_before_its_session_fails_to_resume(
    home: Path, tmp_path: Path
) -> None:
    gone_session = line("runError", stage="init", message="Session saved-session not found")
    runner, _ = make_runner(tmp_path, stdout=[line("runStarted"), gone_session], exit=1)
    runner.set_conversation("agent-1", "saved-session")
    events: list[Event] = []
    with pytest.raises(ResumeFailed, match=r"^Session saved-session not found$"):
        async for event in runner.converse("t", launch_of(PLANNER)):
            events.append(event)
    assert events == [TurnStarted(turn_id="t", agent_id="agent-1")]
    assert configs(home) == []


@pytest.mark.usefixtures("home")
async def test_a_new_turn_failing_to_start_just_fails(tmp_path: Path) -> None:
    failed = line("runError", stage="init", message="Couldn't start")
    runner, _ = make_runner(tmp_path, stdout=[line("runStarted"), failed], exit=1)
    events = await converse(runner, launch_of(PLANNER))
    assert isinstance(events[-1], TurnFinished) and events[-1].reason == "error"


@pytest.mark.usefixtures("home")
@pytest.mark.parametrize(
    "error",
    [
        line("runError", stage="init", sessionId=SESSION_ID, message="Couldn't start"),
        line("runError", stage="prompt", message="Broken prompt"),
    ],
)
async def test_a_resumed_turn_failing_after_its_session_or_later_just_fails(
    tmp_path: Path, error: str
) -> None:
    runner, _ = make_runner(tmp_path, stdout=[line("runStarted"), error], exit=1)
    runner.set_conversation("agent-1", "saved-session")
    events = await converse(runner, launch_of(PLANNER))
    assert isinstance(events[-1], TurnFinished) and events[-1].reason == "error"


# Effort


EFFORT_NOTE = "kiro's model claude-haiku-4.5 ignores effort; coder runs without it."


def notes(events: list[Event]) -> list[str]:
    return [event.message for event in events if isinstance(event, Notice)]


@pytest.mark.usefixtures("home")
async def test_effort_a_run_reports_unavailable_is_noted_once_per_role_per_session(
    tmp_path: Path,
) -> None:
    runner, _ = make_runner(tmp_path, fixture="plain.jsonl")
    settings = Settings(model="claude-haiku-4.5", effort="high")
    assert notes(await run_agent(runner, launch_of(CODER, settings))) == [EFFORT_NOTE]
    second = Agent(id="agent-4", role="coder", task_id="8", token="0ther")
    assert notes(await run_agent(runner, launch_of(second, settings))) == []
    elsewhere = launch_of(second, settings, session_id="session-2")
    assert notes(await run_agent(runner, elsewhere)) == [EFFORT_NOTE]


@pytest.mark.usefixtures("home")
async def test_each_roles_effort_is_noted_naming_kiros_auto_without_a_model(
    tmp_path: Path,
) -> None:
    runner, _ = make_runner(tmp_path, fixture="plain.jsonl")
    await run_agent(runner, launch_of(CODER, Settings(effort="low")))
    events = await converse(runner, launch_of(PLANNER, Settings(effort="max")))
    assert notes(events) == ["kiro's model auto ignores effort; planner runs without it."]


@pytest.mark.usefixtures("home")
@pytest.mark.parametrize(("fixture", "effort"), [("plain.jsonl", None), ("outage.jsonl", "high")])
async def test_effort_is_noted_only_when_set_and_reported_unavailable(
    tmp_path: Path, fixture: str, effort: Effort | None
) -> None:
    runner, _ = make_runner(tmp_path, fixture=fixture)
    assert notes(await run_agent(runner, launch_of(CODER, Settings(effort=effort)))) == []


# What Kiro can do, its models and its failures


def test_kiro_resumes_and_reports_usage_at_every_level_but_caps_no_turns() -> None:
    can = KiroCliRunner.capabilities
    assert (can.resume, can.usage, can.max_turns) == (True, True, False)
    assert can.tool_access == frozenset(get_args(ToolAccess))
    assert can.effort == frozenset(get_args(Effort))


@pytest.mark.parametrize(
    "model", ["glm-5", "claude-sonnet-4.5", "qwen3-coder-next", "minimax-m2.1"]
)
def test_kiros_full_ids_are_lowercase_names_with_a_dash(model: str) -> None:
    assert KiroCliRunner.full_model_ids.matches(model)


@pytest.mark.parametrize("model", ["opus", "auto", "GLM-5", "glm_5", "glm 5"])
def test_names_without_a_dash_or_with_other_characters_arent_kiros_full_ids(model: str) -> None:
    assert not KiroCliRunner.full_model_ids.matches(model)


def test_kiros_failures_are_told_from_its_wording() -> None:
    runner = KiroCliRunner()
    assert runner.failure(fixture_message("outage.jsonl")) == Failure("api_unavailable")
    limit = Failure("usage_limit", retry=KIRO_LIMIT_RETRY)
    assert runner.failure("Monthly credit limit reached") == limit
    assert runner.failure(fixture_message("bad-model.jsonl")) is None


def test_sweeping_removes_the_configs_crashed_runs_left_over_a_day(home: Path) -> None:
    now = datetime(2026, 10, 25, 12, tzinfo=UTC)
    files = KiroAgentFiles()
    old = home / ".kiro" / "agents" / f"{files.write('old', {})}.json"
    new = files.write("new", {})
    stale = (now - timedelta(days=2)).timestamp()
    os.utime(old, (stale, stale))
    KiroCliRunner().sweep(now)
    assert [path.name for path in configs(home)] == [f"{new}.json"]
