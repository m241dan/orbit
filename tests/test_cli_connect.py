"""``orbit chat --connect``: the TUI over a control plane ``orbit serve`` runs."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from orbit.cli import app
from orbit.protocol import Command, NewSession

runner = CliRunner()
URL = "ws://127.0.0.1:7433/"


@dataclass
class FakeClient:
    """The WebSocket session built: where to, shown the token read from which file."""

    url: str
    token_file: Path


@dataclass
class FakeApp:
    """The TUI built, run once."""

    built: ClassVar[list["FakeApp"]] = []

    session: object
    link: object = None
    opening: Command | None = None
    pick: bool = False
    storage_halt: str | None = None
    ran: bool = field(default=False)

    def __post_init__(self) -> None:
        FakeApp.built.append(self)

    def run(self) -> None:
        self.ran = True


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def no_backend(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("A connected chat builds no backend.")

    monkeypatch.setattr("orbit.cli.chat.OrbitApp", FakeApp)
    monkeypatch.setattr("orbit.cli.chat.WebSocketSession", FakeClient)
    monkeypatch.setattr("orbit.cli.chat._backend", no_backend)
    monkeypatch.setenv("ORBIT_CONFIG_DIR", str(tmp_path / "config"))
    FakeApp.built = []


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    """The token file in the config directory."""
    path = tmp_path / "config" / "token"
    path.parent.mkdir()
    path.write_text("the-token\n")
    return path


def test_connect_runs_the_tui_over_the_control_plane_with_the_config_dirs_token(
    token_file: Path,
) -> None:
    result = runner.invoke(app, ["chat", "--connect", URL])
    assert result.exit_code == 0, result.output
    (tui,) = FakeApp.built
    assert tui.session == FakeClient(URL, token_file)
    assert tui.link is tui.session and tui.opening is None and tui.ran
    assert tui.pick  # The user picks a session once joined.


def test_the_token_file_option_names_another_file(tmp_path: Path) -> None:
    other = tmp_path / "other-token"
    other.write_text("another\n")
    result = runner.invoke(app, ["chat", "--connect", URL, "--token-file", str(other)])
    assert result.exit_code == 0, result.output
    assert FakeApp.built[0].session == FakeClient(URL, other)


@pytest.mark.usefixtures("token_file")
def test_new_opens_a_new_session_once_welcomed() -> None:
    assert runner.invoke(app, ["chat", "--connect", URL, "--new"]).exit_code == 0
    assert FakeApp.built[0].opening == NewSession()
    assert not FakeApp.built[0].pick


@pytest.mark.usefixtures("token_file")
def test_name_opens_a_new_session_with_that_name() -> None:
    assert runner.invoke(app, ["chat", "--connect", URL, "--name", "auth"]).exit_code == 0
    assert FakeApp.built[0].opening == NewSession(name="auth")


@pytest.mark.usefixtures("token_file")
@pytest.mark.parametrize(
    ("args", "named"),
    [
        (["--model", "opus"], "--model"),
        (["--tools", "full"], "--tools"),
        (["--no-verify"], "--verify"),
        (["--verify"], "--verify"),
        (["--no-worktree"], "--worktree"),
        (["--no-review"], "--review"),
        (["--no-usage"], "--usage"),
        (["--profile", "/tmp/p"], "--profile"),
    ],
)
def test_a_backend_flag_with_connect_exits_saying_it_belongs_on_serve(
    args: list[str], named: str
) -> None:
    result = runner.invoke(app, ["chat", "--connect", URL, *args])
    assert result.exit_code == 2
    assert f"{named} configure the control plane, so they belong on orbit serve" in result.stderr
    assert FakeApp.built == []


@pytest.mark.usefixtures("token_file")
def test_every_backend_flag_given_is_named() -> None:
    result = runner.invoke(app, ["chat", "--connect", URL, "--model", "opus", "--no-usage"])
    assert result.exit_code == 2
    assert result.stderr.startswith("--model, --usage configure the control plane")


@pytest.mark.usefixtures("token_file")
def test_a_url_other_than_ws_exits() -> None:
    result = runner.invoke(app, ["chat", "--connect", "wss://example.com/"])
    assert result.exit_code == 2
    assert "takes a ws:// URL, not wss://example.com/" in result.stderr
    assert FakeApp.built == []


def test_a_missing_token_file_exits_naming_it(tmp_path: Path) -> None:
    result = runner.invoke(app, ["chat", "--connect", URL])
    assert result.exit_code == 2
    assert f"Can't read the token file {tmp_path / 'config' / 'token'}: " in result.stderr
    assert FakeApp.built == []
