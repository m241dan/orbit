"""``orbit serve``: the control plane on its own, served on loopback until stopped."""

import asyncio
import json
import os
import signal
import socket
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import ClientConnection, connect

from orbit import cli
from orbit.agent import TokenFile, WorkspaceLock, WorkspaceOwner
from orbit.cli import app
from orbit.protocol import (
    PROTOCOL_VERSION,
    AddTask,
    Catalog,
    Command,
    CommandAccepted,
    CommandEnvelope,
    Event,
    Hello,
    Snapshot,
    TaskSpec,
    Welcome,
    decode_event,
    encode,
)

runner = CliRunner()
TASK = TaskSpec(prompt="Add a flag.")
QUIET = ["--no-usage", "--no-verify"]


@pytest.fixture(autouse=True)
def project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """The workspace served, the current directory, put back afterwards."""
    path = tmp_path / "project"
    path.mkdir()
    monkeypatch.chdir(path)
    yield path


@pytest.fixture(autouse=True)
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "state"
    monkeypatch.setenv("ORBIT_STATE_DIR", str(path))
    return path


@pytest.fixture(autouse=True)
def config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "config"
    monkeypatch.setenv("ORBIT_CONFIG_DIR", str(path))
    return path


@dataclass
class Frontend:
    """A frontend run on a thread of its own against the ``orbit serve`` whose run file is in
    ``state``, showing the token in ``config_dir``: it says Hello, does ``work`` and stops the
    server with SIGTERM, noting what it saw."""

    state: Path
    config_dir: Path
    work: Callable[[ClientConnection], list[Event]] = lambda websocket: []
    run_file: dict[str, object] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    close: tuple[int | None, str | None] = (None, None)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()
        return thread

    def _run(self) -> None:
        self.run_file = _run_file(self.state)
        token = (self.config_dir / "token").read_text().strip()
        url = str(self.run_file["url"])
        with connect(url, additional_headers={"Authorization": f"Bearer {token}"}) as ws:
            say(ws, "hello", Hello(client="orbit-tests", protocol_version=PROTOCOL_VERSION))
            self.events = until(ws, Snapshot) + self.work(ws)
            os.kill(os.getpid(), signal.SIGTERM)
            self.close = closed(ws)


def _run_file(state: Path) -> dict[str, object]:
    """The run file in ``state``, once there is one."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        found = list((state / "run").glob("*.json")) if (state / "run").exists() else []
        if found:
            data: dict[str, object] = json.loads(found[0].read_text())
            return data
        time.sleep(0.02)
    raise AssertionError("orbit serve never started")


def say(websocket: ClientConnection, command_id: str, command: Command) -> None:
    websocket.send(encode(CommandEnvelope(command_id=command_id, payload=command)))


def until(websocket: ClientConnection, kind: type[Event]) -> list[Event]:
    events: list[Event] = []
    while not events or not isinstance(events[-1], kind):
        envelope = decode_event(str(websocket.recv(timeout=10)))
        assert envelope is not None
        events.append(envelope.payload)
    return events


def closed(websocket: ClientConnection) -> tuple[int | None, str | None]:
    with pytest.raises(ConnectionClosed):
        while True:
            websocket.recv(timeout=10)
    return websocket.close_code, websocket.close_reason


def adds_a_task(websocket: ClientConnection) -> list[Event]:
    say(websocket, "add", AddTask(task=TASK))
    return until(websocket, CommandAccepted)


def served(frontend: Frontend, *args: str) -> str:
    """What ``orbit serve`` with ``args`` printed, serving ``frontend`` until it stops it."""
    thread = frontend.start()
    result = runner.invoke(app, ["serve", *QUIET, *args])
    thread.join(10)
    assert result.exit_code == 0, result.output
    return result.output


def test_serve_welcomes_a_frontend_with_the_token_until_stopped(
    project: Path, state: Path, config_dir: Path
) -> None:
    frontend = Frontend(state, config_dir)
    output = served(frontend, "--port", "0")

    assert [type(e) for e in frontend.events] == [Welcome, Catalog, Snapshot]
    url = str(frontend.run_file["url"])
    assert url.startswith("ws://127.0.0.1:") and not url.endswith(":0/")
    assert frontend.run_file["pid"] == os.getpid()
    assert frontend.close == (1001, "orbit serve is shutting down")
    assert not list((state / "run").iterdir())
    token = config_dir / "token"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    lines = output.splitlines()
    assert lines[0] == (
        f"Serving {project} at {url} as its owner; frontends show the token in {token}."
    )
    assert lines[1].startswith("orbit-tests connected from 127.0.0.1:")
    assert lines[2].startswith("orbit-tests disconnected from 127.0.0.1:")
    assert lines[3:] == ["orbit serve stopped."]
    assert token.read_text().strip() not in output


def test_serve_names_paths_with_braces_verbatim(
    monkeypatch: pytest.MonkeyPatch, state: Path, tmp_path: Path
) -> None:
    project = tmp_path / "proj{1}"
    project.mkdir()
    config = tmp_path / "{x}"
    monkeypatch.setenv("ORBIT_CONFIG_DIR", str(config))
    frontend = Frontend(state, config)
    output = served(frontend, "--port", "0", "-C", str(project))

    assert [type(e) for e in frontend.events] == [Welcome, Catalog, Snapshot]
    url = frontend.run_file["url"]
    assert output.splitlines()[0] == (
        f"Serving {project} at {url} as its owner; frontends show the token in {config / 'token'}."
    )


def test_serve_stops_if_it_cant_write_its_run_file_naming_it(state: Path) -> None:
    state.mkdir()
    (state / "run").write_text("not a directory\n")
    result = runner.invoke(app, ["serve", *QUIET, "--port", "0"])

    assert result.exit_code == 2
    assert f"Can't write the run file {state / 'run'}" in result.stderr
    assert "Traceback" not in result.output
    assert not [t for t in threading.enumerate() if t.name.startswith("orbit-sqlite")]


def test_serve_names_its_url_in_the_workspace_lock_while_it_owns_the_repository(
    project: Path, state: Path, config_dir: Path
) -> None:
    owners: list[WorkspaceOwner] = []

    def reads_the_lock(websocket: ClientConnection) -> list[Event]:
        owners.append(WorkspaceLock(project / ".orbit").owner())
        return []

    frontend = Frontend(state, config_dir, reads_the_lock)
    served(frontend, "--port", "0")
    assert owners == [WorkspaceOwner(pid=os.getpid(), url=str(frontend.run_file["url"]))]
    after = WorkspaceLock(project / ".orbit")
    assert after.take() is None
    after.close()


def test_serve_on_a_repository_another_orbit_owns_is_read_only_naming_it(
    project: Path, state: Path, config_dir: Path
) -> None:
    owner = WorkspaceLock(project / ".orbit")
    owner.take()
    try:
        frontend = Frontend(state, config_dir)
        output = served(frontend, "--port", "0")
    finally:
        owner.close()

    [snapshot] = [e for e in frontend.events if isinstance(e, Snapshot)]
    assert snapshot.read_only
    url, token = frontend.run_file["url"], config_dir / "token"
    assert output.splitlines()[0] == (
        f"Serving {project} at {url} read-only, as an orbit chat, pid {os.getpid()} owns it;"
        f" frontends show the token in {token}."
    )


def test_serve_takes_its_port_from_the_config_file(state: Path, config_dir: Path) -> None:
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("[server]\nport = 0\n")
    frontend = Frontend(state, config_dir)
    served(frontend)
    assert not str(frontend.run_file["url"]).endswith(":7433/")


def test_serve_opens_the_most_recently_active_session_unless_told_to_start_anew(
    state: Path, config_dir: Path
) -> None:
    served(Frontend(state, config_dir, adds_a_task), "--port", "0")

    resumed = Frontend(state, config_dir)
    served(resumed, "--port", "0")
    fresh = Frontend(state, config_dir)
    served(fresh, "--port", "0", "--new")

    [snapshot] = [e for e in resumed.events if isinstance(e, Snapshot)]
    assert [t.task for t in snapshot.tasks] == [TASK]
    [snapshot] = [e for e in fresh.events if isinstance(e, Snapshot)]
    assert snapshot.tasks == ()


def test_a_chat_discovers_the_control_plane_serving_its_workspace(
    project: Path, state: Path, config_dir: Path
) -> None:
    found: list[str | None] = []

    def discovers(websocket: ClientConnection) -> list[Event]:
        found.append(cli.discover_control_plane(state, project, config_dir / "token"))
        return []

    frontend = Frontend(state, config_dir, discovers)
    served(frontend, "--port", "0")
    assert found == [frontend.run_file["url"]]


def test_serve_can_profile_itself(state: Path, config_dir: Path, tmp_path: Path) -> None:
    served(Frontend(state, config_dir), "--port", "0", "--profile", str(tmp_path / "profile"))
    names = sorted(p.name for p in (tmp_path / "profile").iterdir())
    assert names == ["serve-blocked.log", "serve.prof"]


def test_serve_refuses_a_host_beyond_loopback() -> None:
    result = runner.invoke(app, ["serve", *QUIET, "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "not on 0.0.0.0" in result.output and "TLS" in result.output


def test_serve_refuses_a_port_in_use_naming_it(config_dir: Path) -> None:
    with socket.create_server(("127.0.0.1", 0)) as taken:
        port = taken.getsockname()[1]
        result = runner.invoke(app, ["serve", *QUIET, "--port", str(port)])
    assert result.exit_code == 2
    assert f"Can't listen on port {port}" in result.output
    assert "--port" in result.output and str(config_dir / "config.toml") in result.output


def test_serve_refuses_an_invalid_server_setting(config_dir: Path) -> None:
    config_dir.mkdir()
    path = config_dir / "config.toml"
    path.write_text("[server]\nport = 'high'\n")
    result = runner.invoke(app, ["serve", *QUIET])
    assert result.exit_code == 2
    assert f"{path}: server.port: " in result.output


def test_a_rotated_token_admits_new_frontends_at_once_and_keeps_open_ones(
    state: Path, config_dir: Path
) -> None:
    statuses: list[int] = []
    frontend = Frontend(state, config_dir)

    def rotates(websocket: ClientConnection) -> list[Event]:
        old = (config_dir / "token").read_text().strip()
        TokenFile(config_dir).rotate()
        new = (config_dir / "token").read_text().strip()
        url = str(frontend.run_file["url"])
        with pytest.raises(InvalidStatus) as refused:
            connect(url, additional_headers={"Authorization": f"Bearer {old}"})
        statuses.append(refused.value.response.status_code)
        with connect(url, additional_headers={"Authorization": f"Bearer {new}"}) as other:
            say(other, "hello", Hello(client="orbit-tests", protocol_version=PROTOCOL_VERSION))
            until(other, Snapshot)
        return adds_a_task(websocket)

    frontend.work = rotates
    served(frontend, "--port", "0")

    assert statuses == [401]
    assert isinstance(frontend.events[-1], CommandAccepted)


def test_serve_refuses_a_token_file_others_can_read(config_dir: Path) -> None:
    config_dir.mkdir()
    token = config_dir / "token"
    token.write_text("secret\n")
    token.chmod(0o644)
    result = runner.invoke(app, ["serve", *QUIET, "--port", "0"])
    assert result.exit_code == 2
    assert f"chmod 600 {token}" in result.output


async def test_the_first_signal_stops_serving_and_a_second_ctrl_c_exits_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", exits.append)
    stopping = asyncio.Event()
    cli._on_signals(stopping)
    try:
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(stopping.wait(), 5)
        signal.raise_signal(signal.SIGINT)
        while not exits:
            await asyncio.sleep(0.01)
    finally:
        loop = asyncio.get_running_loop()
        for number in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(number)
    assert exits == [130]
