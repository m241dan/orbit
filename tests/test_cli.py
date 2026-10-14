from typing import ClassVar

import pytest
from typer.testing import CliRunner

from orbit import __version__
from orbit.agent import (
    DEFAULT_STEPS,
    MODEL_ALIASES,
    AgentSession,
    ClaudeCliRunner,
    Limits,
    McpHost,
    Verification,
    run_step,
    workspace_state,
)
from orbit.cli import app
from orbit.protocol import Settings

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_hello_default() -> None:
    result = runner.invoke(app, ["hello"])
    assert result.exit_code == 0
    assert "Hello, world!" in result.stdout


def test_hello_name() -> None:
    result = runner.invoke(app, ["hello", "Ada"])
    assert result.exit_code == 0
    assert "Hello, Ada!" in result.stdout


class FakeApp:
    launched: ClassVar[list[AgentSession]] = []

    def __init__(self, session: AgentSession) -> None:
        self.session = session

    def run(self) -> None:
        FakeApp.launched.append(self.session)


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[AgentSession]:
    monkeypatch.setattr("orbit.cli.OrbitApp", FakeApp)
    FakeApp.launched = []
    return FakeApp.launched


def test_chat_runs_tui_against_claude_cli(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    [session] = launched
    assert isinstance(session.runner, ClaudeCliRunner)
    assert session.settings == Settings()
    assert session.model_choices == MODEL_ALIASES


def test_chat_accepts_model_and_tool_access(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat", "--model", "opus", "--tools", "full"])

    assert result.exit_code == 0
    [session] = launched
    assert session.settings == Settings(model="opus", tool_access="full")


def test_chat_rejects_unknown_tool_access(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat", "--tools", "sometimes"])

    assert result.exit_code == 2
    assert launched == []


def test_chat_verifies_tasks_by_default(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    [session] = launched
    # The default checks run as processes; tasks that change no files in the working tree
    # skip them.
    assert session.verification == Verification(DEFAULT_STEPS, run_step, workspace_state)


def test_chat_can_turn_verification_off(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat", "--no-verify"])

    assert result.exit_code == 0
    [session] = launched
    assert session.verification is None


def test_chat_runs_tasks_through_agents_connected_over_mcp(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    [session] = launched
    agents = session.agents
    assert agents is not None
    assert isinstance(agents.host, McpHost)
    assert isinstance(agents.launcher, ClaudeCliRunner)
    assert agents.launcher is not session.runner
    assert agents.limits == Limits(max_attempts=5)
    first, second = agents.new_token(), agents.new_token()
    assert first != second and len(first) >= 32


def test_chat_sets_the_fixes_an_agent_gets(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat", "--fix-attempts", "2"])

    assert result.exit_code == 0
    [session] = launched
    assert session.agents is not None
    assert session.agents.limits == Limits(max_attempts=2)


def test_chat_rejects_fewer_than_one_fix_attempt(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat", "--fix-attempts", "0"])

    assert result.exit_code == 2
    assert launched == []
