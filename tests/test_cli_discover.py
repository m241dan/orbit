"""``orbit chat`` finding the ``orbit serve`` running for its workspace, and ``--local``."""

import asyncio
import os
import ssl
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pytest
import typer
from typer.testing import CliRunner

from orbit.agent import LocalTls, RunFile
from orbit.cli import app
from orbit.client import TokenRejected
from orbit.protocol import NewSession
from orbit.tui import Opening
from tests.store_views import open_view
from tests.tls_certs import ServerFiles, make_ca, make_client

runner = CliRunner()
URL = "ws://127.0.0.1:7433/"


@dataclass
class FakeClient:
    """The WebSocket session built: where to, shown the token read from which file, verified
    with which context."""

    url: str
    token_file: Path | None
    ssl_context: ssl.SSLContext | None = None


@dataclass
class FakeApp:
    """The TUI built over a control plane, run once."""

    built: ClassVar[list["FakeApp"]] = []

    session: object
    link: object = None
    opening: Opening = field(default_factory=Opening)
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
    shown: list[tuple[str, str | None]] = field(default_factory=list)
    contexts: list[ssl.SSLContext | None] = field(default_factory=list)

    async def accepts(
        self, url: str, token: str | None, ssl_context: ssl.SSLContext | None = None
    ) -> bool:
        self.shown.append((url, token))
        self.contexts.append(ssl_context)
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
    monkeypatch.setattr("orbit.cli.chat.accepts", fake.accepts)
    return fake


@pytest.fixture
def in_process(monkeypatch: pytest.MonkeyPatch) -> InProcess:
    fake = InProcess()
    monkeypatch.setattr("orbit.cli.chat._backend", fake)
    monkeypatch.setattr("orbit.cli.chat.OrbitApp", FakeApp)
    monkeypatch.setattr("orbit.cli.chat.WebSocketSession", FakeClient)
    FakeApp.built = []
    return fake


def serving(
    state: Path,
    workspace: Path,
    pid: int | None = None,
    url: str = URL,
    local: LocalTls | None = None,
) -> None:
    """Say, in a run file in ``state``, that an orbit serve in process ``pid`` (this one, by
    default) serves ``workspace`` at ``url``, verified as ``local`` says if given."""

    async def registered() -> str:
        store = await open_view(state, workspace)
        await store.close()
        return store.workspace_id

    RunFile(state, asyncio.run(registered())).write(url, pid or os.getpid(), local)


def test_chat_connects_to_the_control_plane_serving_the_workspace(
    project: Path, state: Path, token_file: Path, in_process: InProcess, plane: Plane
) -> None:
    serving(state, project)
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 0, result.output
    assert result.output == ""
    (tui,) = FakeApp.built
    assert tui.session == FakeClient(URL, token_file)
    assert tui.opening.command is None and tui.ran
    assert tui.opening.repository == str(project.resolve())
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
    assert FakeApp.built[0].opening.command == opening


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


TLS_URL = "wss://0.0.0.0:7433/"
LOCAL_URL = "wss://localhost:7433/"


@pytest.fixture
def ca_path(tmp_path: Path) -> Path:
    return make_ca(tmp_path).path


@pytest.fixture
def client(tmp_path: Path) -> ServerFiles:
    """A client certificate signed by a CA of its own, kept apart from the server's."""
    directory = tmp_path / "client"
    directory.mkdir()
    return make_client(make_ca(directory), directory)


@pytest.fixture
def serving_tls(project: Path, state: Path, ca_path: Path) -> Callable[[bool], None]:
    """What says, in a run file, that an orbit serve serves the project over TLS at
    ``TLS_URL``, mutual TLS if asked, its certificate giving ``localhost``."""

    def serve(mutual: bool) -> None:
        local = LocalTls(mutual=mutual, ca=str(ca_path), hostname="localhost")
        serving(state, project, url=TLS_URL, local=local)

    return serve


def _local_client(token_file: Path, cert: Path, key: Path) -> None:
    """Name, in ``[server]`` in the config file beside ``token_file``, the client certificate
    ``cert`` and its ``key`` frontends here show."""
    (token_file.parent / "config.toml").write_text(
        f'[server]\nlocal_client_cert = "{cert}"\nlocal_client_key = "{key}"\n'
    )


def test_chat_connects_to_a_control_plane_serving_tls_by_the_ca_and_hostname_it_recorded(
    token_file: Path, serving_tls: Callable[[bool], None], in_process: InProcess, plane: Plane
) -> None:
    serving_tls(False)
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 0, result.output
    (tui,) = FakeApp.built
    assert isinstance(tui.session, FakeClient)
    assert (tui.session.url, tui.session.token_file) == (LOCAL_URL, token_file)
    assert isinstance(tui.session.ssl_context, ssl.SSLContext)
    assert plane.shown == [(LOCAL_URL, "the-token")]
    assert all(isinstance(context, ssl.SSLContext) for context in plane.contexts)
    assert in_process.built == []


def test_chat_shows_a_control_plane_serving_mtls_the_local_client_certificate(
    token_file: Path,
    client: ServerFiles,
    serving_tls: Callable[[bool], None],
    in_process: InProcess,
    plane: Plane,
) -> None:
    _local_client(token_file, client.cert, client.key)
    serving_tls(True)
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 0, result.output
    (tui,) = FakeApp.built
    assert isinstance(tui.session, FakeClient)
    assert (tui.session.url, tui.session.token_file) == (LOCAL_URL, None)
    assert plane.shown == [(LOCAL_URL, None)]
    assert in_process.built == []


@pytest.mark.usefixtures("plane")
def test_without_a_local_client_certificate_chat_stops_naming_the_keys_it_needs(
    token_file: Path, serving_tls: Callable[[bool], None], in_process: InProcess
) -> None:
    serving_tls(True)
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 2
    assert result.stderr.startswith("Local discovery under mutual TLS needs a client certificate.")
    assert "local_client_cert and local_client_key" in result.stderr
    assert str(token_file.parent / "config.toml") in result.stderr
    assert in_process.built == [] and FakeApp.built == []


@pytest.mark.usefixtures("plane")
def test_a_local_client_key_others_can_read_stops_chat_with_the_chmod_that_fixes_it(
    token_file: Path,
    client: ServerFiles,
    serving_tls: Callable[[bool], None],
    in_process: InProcess,
) -> None:
    client.key.chmod(0o644)
    _local_client(token_file, client.cert, client.key)
    serving_tls(True)
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 2
    assert f"chmod 600 {client.key}" in result.stderr
    assert in_process.built == [] and FakeApp.built == []


@pytest.mark.usefixtures("plane")
def test_an_invalid_config_under_mtls_stops_chat_naming_it(
    token_file: Path, serving_tls: Callable[[bool], None], in_process: InProcess
) -> None:
    (token_file.parent / "config.toml").write_text("[server]\nlocal_client_cert = 1\n")
    serving_tls(True)
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 2
    assert "server.local_client_cert" in result.stderr
    assert in_process.built == [] and FakeApp.built == []
