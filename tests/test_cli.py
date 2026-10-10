import pytest
from typer.testing import CliRunner

from orbit import __version__
from orbit.agent import AgentSession
from orbit.cli import app

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


def test_chat_runs_tui_with_agent_session(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[object] = []

    class FakeApp:
        def __init__(self, session: object) -> None:
            self.session = session

        def run(self) -> None:
            launched.append(self.session)

    monkeypatch.setattr("orbit.cli.OrbitApp", FakeApp)
    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    assert len(launched) == 1
    assert isinstance(launched[0], AgentSession)
