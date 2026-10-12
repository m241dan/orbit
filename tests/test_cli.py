from typing import ClassVar

import pytest
from typer.testing import CliRunner

from orbit import __version__
from orbit.agent import AgentSession, ClaudeCliRunner
from orbit.agent.claude_cli import MODEL_ALIASES
from orbit.agent.verify import DEFAULT_STEPS
from orbit.agent.workspace import workspace_state
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
    assert session.verification_steps == DEFAULT_STEPS
    # Tasks that change no files in the working tree skip verification.
    assert session.workspace is workspace_state


def test_chat_can_turn_verification_off(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat", "--no-verify"])

    assert result.exit_code == 0
    [session] = launched
    assert session.verification_steps == ()
