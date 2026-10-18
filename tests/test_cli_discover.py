"""``orbit chat`` finding the ``orbit serve`` running for its workspace, and ``--local``."""

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pytest
import typer
from typer.testing import CliRunner

from orbit.agent import RunFile, SqliteStore
from orbit.cli import app
from orbit.client import TokenRejected
from orbit.protocol import Command, NewSession

runner = CliRunner()
URL = "ws://127.0.0.1:7433/"


@dataclass
class FakeClient:
    """The WebSocket session built: where to, with which token, read from which file."""

    url: str
    token: str
    token_file: Path


@dataclass
class FakeApp:
    """The TUI built over a control plane, run once."""

    built: ClassVar[list["FakeApp"]] = []

    session: object
    link: object = None
    opening: Command | None = None
    storage_halt: str | None = None
    ran: bool = field(default=False)

    def __post_init__(self) -> None:
        FakeApp.built.append(self)

    def run(self) -> None:
        self.ran = True


@dataclass
class Plane:
    """What the control plane asked says of the token: ``answer`` takes it if true, can't be
    reached if false, and turns it away if ``None``; and the tokens it was shown."""

    answer: bool | None = True
    shown: list[tuple[str, str]] = field(default_factory=list)

    async def accepts(self, url: str, token: str) -> bool:
        self.shown.append((url, token))
        if self.answer is None:
            raise TokenRejected()
        return self.answer


@dataclass
class InProcess:
    """The in-process backends built, each stopping chat before it runs."""

    built: list[Any] = field(default_factory=list)

    def __call__(self, backend: Any, *args: Any, **kwargs: Any) -> None:
        self.built.append(backend)
        raise typer.Exit(0)


@pytest.fixture(autouse=True)
def project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """The workspace chat runs in, the current directory, put back afterwards."""
    path = tmp_path / "project"
    path.mkdir()
    monkeypatch.chdir(path)
    yield path


@pytest.fixture(autouse=True)
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "state"
    monkeypatch.setenv("ORBIT_STATE_DIR", str(path))
    return path


@pytest.fixture
def token_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The token file in the config directory."""
    monkeypatch.setenv("ORBIT_CONFIG_DIR", str(tmp_path / "config"))
    path = tmp_path / "config" / "token"
    path.parent.mkdir()
    path.write_text("the-token\n")
    return path


@pytest.fixture
def plane(monkeypatch: pytest.MonkeyPatch) -> Plane:
    fake = Plane()
    monkeypatch.setattr("orbit.cli.accepts", fake.accepts)
    return fake


@pytest.fixture
def in_process(monkeypatch: pytest.MonkeyPatch) -> InProcess:
    fake = InProcess()
    monkeypatch.setattr("orbit.cli._backend", fake)
    monkeypatch.setattr("orbit.cli.OrbitApp", FakeApp)
    monkeypatch.setattr("orbit.cli.WebSocketSession", FakeClient)
    FakeApp.built = []
    return fake


def serving(state: Path, workspace: Path, pid: int | None = None) -> None:
    """Say, in a run file in ``state``, that an orbit serve in process ``pid`` (this one, by
    default) serves ``workspace`` at ``URL``."""

    async def registered() -> str:
        store = await SqliteStore.open(state, str(workspace))
        await store.close()
        return store.workspace_id

    RunFile(state, asyncio.run(registered())).write(URL, pid or os.getpid())


def test_chat_connects_to_the_control_plane_serving_the_workspace(
    project: Path, state: Path, token_file: Path, in_process: InProcess, plane: Plane
) -> None:
    serving(state, project)
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 0, result.output
    assert result.output == ""
    (tui,) = FakeApp.built
    assert tui.session == FakeClient(URL, "the-token", token_file)
    assert tui.opening is None and tui.ran
    assert plane.shown == [(URL, "the-token")]
    assert in_process.built == []


@pytest.mark.usefixtures("plane", "token_file")
@pytest.mark.parametrize(
    ("args", "opening"),
    [(["--new"], NewSession()), (["--name", "auth"], NewSession(name="auth"))],
)
def test_new_and_name_open_a_new_session_once_joined(
    project: Path, state: Path, args: list[str], opening: NewSession, in_process: InProcess
) -> None:
    serving(state, project)
    assert runner.invoke(app, ["chat", *args]).exit_code == 0
    assert FakeApp.built[0].opening == opening


@pytest.mark.usefixtures("plane", "token_file")
def test_local_runs_in_process_with_the_backend_flags(
    project: Path, state: Path, in_process: InProcess
) -> None:
    serving(state, project)
    result = runner.invoke(app, ["chat", "--local", "--model", "opus"])
    assert result.exit_code == 0, result.output
    assert [backend.model for backend in in_process.built] == ["opus"]
    assert FakeApp.built == []


@pytest.mark.usefixtures("token_file")
def test_a_refused_token_stops_chat_naming_the_token_file(
    project: Path, state: Path, plane: Plane, token_file: Path, in_process: InProcess
) -> None:
    serving(state, project)
    plane.answer = None
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 2
    assert f"The control plane serving {project} refused the token from {token_file}" in (
        result.stderr
    )
    assert "--local" in result.stderr
    assert in_process.built == [] and FakeApp.built == []


@pytest.mark.usefixtures("plane")
def test_an_unreadable_token_file_stops_chat_naming_it(
    project: Path, state: Path, tmp_path: Path, in_process: InProcess
) -> None:
    serving(state, project)
    missing = tmp_path / "missing"
    result = runner.invoke(app, ["chat", "--token-file", str(missing)])
    assert result.exit_code == 2
    assert f"Can't read the token file {missing}: " in result.stderr
    assert in_process.built == [] and FakeApp.built == []


@pytest.mark.usefixtures("plane", "token_file")
def test_a_backend_flag_with_a_control_plane_found_says_it_belongs_on_serve_or_local(
    project: Path, state: Path, in_process: InProcess
) -> None:
    serving(state, project)
    result = runner.invoke(app, ["chat", "--model", "opus"])
    assert result.exit_code == 2
    assert result.stderr.startswith(
        f"--model configure the control plane, so they belong on orbit serve, which serves"
        f" {project} at {URL}; or pass --local"
    )
    assert in_process.built == [] and FakeApp.built == []


@pytest.mark.usefixtures("token_file")
def test_only_another_workspaces_control_plane_leaves_chat_in_process(
    project: Path, state: Path, tmp_path: Path, plane: Plane, in_process: InProcess
) -> None:
    serving(state, tmp_path)
    assert runner.invoke(app, ["chat"]).exit_code == 0
    assert len(in_process.built) == 1
    assert plane.shown == []


@pytest.mark.usefixtures("token_file")
def test_a_run_file_whose_process_is_gone_leaves_chat_in_process(
    project: Path, state: Path, plane: Plane, in_process: InProcess
) -> None:
    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()
    serving(state, project, process.pid)
    assert runner.invoke(app, ["chat"]).exit_code == 0
    assert len(in_process.built) == 1
    assert plane.shown == []


@pytest.mark.usefixtures("token_file")
def test_a_control_plane_that_cant_be_reached_leaves_chat_in_process(
    project: Path, state: Path, plane: Plane, in_process: InProcess
) -> None:
    serving(state, project)
    plane.answer = False
    assert runner.invoke(app, ["chat", "--model", "opus"]).exit_code == 0
    assert [backend.model for backend in in_process.built] == ["opus"]


@pytest.mark.usefixtures("token_file")
def test_local_and_connect_together_stop_chat(in_process: InProcess) -> None:
    result = runner.invoke(app, ["chat", "--connect", URL, "--local"])
    assert result.exit_code == 2
    assert "--local runs the control plane here, so it can't go with --connect." in result.stderr
    assert FakeApp.built == []
