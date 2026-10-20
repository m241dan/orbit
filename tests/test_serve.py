"""``orbit serve``: the control plane on its own, served on loopback until stopped."""

import asyncio
import json
import os
import queue
import re
import signal
import socket
import ssl
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

from orbit.agent import ControlPlaneServer, RunFile, TokenFile, WorkspaceLock, WorkspaceOwner
from orbit.agent.domain.server import client_fingerprint
from orbit.agent.external.sqlite_store import find_workspace_id
from orbit.cli import app, chat, serve
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
from tests.tls_certs import DAY, Ca, ServerFiles, make_ca, make_client, make_server, other_key

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
    listening: Callable[[Path], dict[str, object]] = lambda state: _run_file(state)
    run_file: dict[str, object] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    close: tuple[int | None, str | None] = (None, None)
    trust: ssl.SSLContext | None = None

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()
        return thread

    def _run(self) -> None:
        self.run_file = self.listening(self.state)
        token = (self.config_dir / "token").read_text().strip()
        # A server listening on every address is reached on the one its certificate names.
        url = str(self.run_file["url"]).replace("//0.0.0.0:", "//localhost:")
        headers = {"Authorization": f"Bearer {token}"}
        with connect(url, additional_headers=headers, ssl=self.trust) as ws:
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


def _listening_urls(monkeypatch: pytest.MonkeyPatch) -> Callable[[Path], dict[str, object]]:
    """What gives, once ``orbit serve`` listens, its URL as a run file would, for a serve that
    writes none."""
    urls: queue.Queue[str] = queue.Queue()
    start = ControlPlaneServer.start

    async def recording(server: ControlPlaneServer) -> str:
        url = await start(server)
        urls.put(url)
        return url

    monkeypatch.setattr(ControlPlaneServer, "start", recording)
    return lambda state: {"url": urls.get(timeout=30)}


def test_serve_on_a_repository_another_orbit_owns_is_read_only_naming_it(
    monkeypatch: pytest.MonkeyPatch, project: Path, state: Path, config_dir: Path
) -> None:
    owner = WorkspaceLock(project / ".orbit")
    owner.take()
    try:
        frontend = Frontend(state, config_dir, listening=_listening_urls(monkeypatch))
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


def test_a_read_only_serve_leaves_the_owners_run_file_as_it_was(
    monkeypatch: pytest.MonkeyPatch, project: Path, state: Path, config_dir: Path
) -> None:
    served(Frontend(state, config_dir), "--port", "0")
    workspace_id = find_workspace_id(state, str(project))
    assert workspace_id is not None
    owners_file = RunFile(state, workspace_id)
    owners_file.write("ws://127.0.0.1:7433/", 1)
    before = owners_file.path.read_text()
    seen: list[str] = []

    def reads_the_run_files(websocket: ClientConnection) -> list[Event]:
        seen.extend(path.read_text() for path in (state / "run").iterdir())
        return []

    owner = WorkspaceLock(project / ".orbit")
    owner.take()
    try:
        frontend = Frontend(state, config_dir, reads_the_run_files, _listening_urls(monkeypatch))
        served(frontend, "--port", "0")
    finally:
        owner.close()

    assert seen == [before]
    assert list((state / "run").iterdir()) == [owners_file.path]
    assert owners_file.path.read_text() == before


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
    found: list[chat.Discovered | None] = []

    def discovers(websocket: ClientConnection) -> list[Event]:
        found.append(chat.discover_control_plane(state, project, config_dir / "token", config_dir))
        return []

    frontend = Frontend(state, config_dir, discovers)
    served(frontend, "--port", "0")
    [discovered] = found
    assert discovered is not None and discovered.url == frontend.run_file["url"]


def test_serve_can_profile_itself(state: Path, config_dir: Path, tmp_path: Path) -> None:
    served(Frontend(state, config_dir), "--port", "0", "--profile", str(tmp_path / "profile"))
    names = sorted(p.name for p in (tmp_path / "profile").iterdir())
    assert names == ["serve-blocked.log", "serve.prof"]


def test_serve_refuses_a_host_beyond_loopback_without_tls() -> None:
    result = runner.invoke(app, ["serve", *QUIET, "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert result.stderr.startswith("Plaintext is for loopback alone.")
    assert "0.0.0.0" in result.stderr and "--tls tls" in result.stderr


def _tls_flags(files: ServerFiles) -> list[str]:
    return ["--tls", "tls", "--cert", str(files.cert), "--key", str(files.key)]


def test_serve_over_tls_welcomes_a_frontend_trusting_its_ca_beyond_loopback(
    state: Path, config_dir: Path, tmp_path: Path
) -> None:
    ca = make_ca(tmp_path)
    files = make_server(ca, tmp_path)
    frontend = Frontend(state, config_dir, trust=ssl.create_default_context(cafile=ca.path))
    output = served(frontend, *_tls_flags(files), "--host", "0.0.0.0", "--port", "0")

    assert [type(e) for e in frontend.events] == [Welcome, Catalog, Snapshot]
    assert str(frontend.run_file["url"]).startswith("wss://0.0.0.0:")
    assert "orbit-tests connected from" in output


def _discovering(
    state: Path, project: Path, config_dir: Path, found: list[chat.Discovered | None]
) -> Callable[[ClientConnection], list[Event]]:
    """What has a chat in ``project`` discover the control plane serving it, noting what it found
    in ``found``."""

    def discovers(websocket: ClientConnection) -> list[Event]:
        found.append(chat.discover_control_plane(state, project, config_dir / "token", config_dir))
        return []

    return discovers


def test_a_chat_discovers_a_control_plane_serving_tls_trusting_the_ca_in_its_run_file(
    project: Path, state: Path, config_dir: Path, tmp_path: Path
) -> None:
    ca = make_ca(tmp_path)
    files = make_server(ca, tmp_path)
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(f'[server]\nca = "{ca.path}"\n')
    found: list[chat.Discovered | None] = []
    trust = ssl.create_default_context(cafile=ca.path)
    frontend = Frontend(
        state, config_dir, _discovering(state, project, config_dir, found), trust=trust
    )
    output = served(frontend, *_tls_flags(files), "--host", "0.0.0.0", "--port", "0")

    port = str(frontend.run_file["url"]).rsplit(":", 1)[1]
    assert frontend.run_file["tls"] == {
        "mutual": False,
        "ca": str(ca.path),
        "hostname": "localhost",
    }
    [discovered] = found
    assert discovered is not None
    assert discovered.url == f"wss://localhost:{port}"
    assert discovered.files.ca == ca.path and discovered.files.cert is None
    assert "Warning" not in output


def test_a_tls_serve_without_a_ca_says_local_discovery_wont_work(
    state: Path, config_dir: Path, tmp_path: Path
) -> None:
    ca = make_ca(tmp_path)
    frontend = Frontend(state, config_dir, trust=ssl.create_default_context(cafile=ca.path))
    output = served(frontend, *_tls_flags(make_server(ca, tmp_path)), "--port", "0")

    assert "tls" not in frontend.run_file
    assert "Warning: Local discovery won't work without a CA." in output


def _mtls_config(config_dir: Path, ca: Ca, files: ServerFiles, revoked: str = "") -> None:
    """Have the config file in ``config_dir`` serve mutual TLS with ``files`` and the client CA
    ``ca``, revoking ``revoked`` if given."""
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text(
        f'[server]\nport = 0\ntls = "mtls"\ncert = "{files.cert}"\nkey = "{files.key}"\n'
        f'client_ca = "{ca.path}"\nrevoked = [{f'"{revoked}"' if revoked else ""}]\n'
    )


def _trusting(ca: Ca, client: ServerFiles) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=ca.path)
    context.load_cert_chain(client.cert, client.key)
    return context


def test_serve_over_mtls_names_the_client_certificate_and_revokes_it_without_restarting(
    state: Path, config_dir: Path, tmp_path: Path
) -> None:
    ca = make_ca(tmp_path)
    files = make_server(ca, tmp_path)
    client = make_client(ca, tmp_path)
    fingerprint = client_fingerprint(ssl.PEM_cert_to_DER_cert(client.cert.read_text()))
    _mtls_config(config_dir, ca, files)
    statuses: list[int] = []
    frontend = Frontend(state, config_dir, trust=_trusting(ca, client))

    def revokes(websocket: ClientConnection) -> list[Event]:
        _mtls_config(config_dir, ca, files, revoked=fingerprint)
        with pytest.raises(InvalidStatus) as refused:
            connect(str(frontend.run_file["url"]), ssl=frontend.trust)
        statuses.append(refused.value.response.status_code)
        return adds_a_task(websocket)

    frontend.work = revokes
    output = served(frontend)

    assert statuses == [403]
    assert f"as CN=laptop ({fingerprint})." in output
    assert f"frontends show a client certificate {ca.path} signed." in output


def test_a_chat_discovers_a_control_plane_serving_mtls_showing_the_local_client_certificate(
    project: Path, state: Path, config_dir: Path, tmp_path: Path
) -> None:
    ca = make_ca(tmp_path)
    files = make_server(ca, tmp_path)
    client = make_client(ca, tmp_path)
    _mtls_config(config_dir, ca, files)
    with (config_dir / "config.toml").open("a") as config:
        config.write(f'local_client_cert = "{client.cert}"\nlocal_client_key = "{client.key}"\n')
    found: list[chat.Discovered | None] = []
    discovers = _discovering(state, project, config_dir, found)
    frontend = Frontend(state, config_dir, discovers, trust=_trusting(ca, client))
    served(frontend)

    assert frontend.run_file["tls"] == {"mutual": True, "ca": str(ca.path), "hostname": "127.0.0.1"}
    [discovered] = found
    assert discovered is not None
    assert discovered.files.token_file is None
    assert (discovered.files.cert, discovered.files.key) == (client.cert, client.key)


def test_while_the_config_is_invalid_the_last_revoked_fingerprints_stay(
    config_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_dir.mkdir()
    path = config_dir / "config.toml"
    path.write_text('[server]\nrevoked = ["AB:CD"]\n')
    revoked = serve._current_revoked(config_dir, ())
    assert revoked() == ("AB:CD",)
    path.write_text("[server]\nrevoked = 'all'\n")
    assert revoked() == ("AB:CD",)
    assert "server.revoked" in caplog.text


def test_serve_takes_its_tls_settings_from_the_config_file(
    state: Path, config_dir: Path, tmp_path: Path
) -> None:
    ca = make_ca(tmp_path)
    files = make_server(ca, tmp_path, valid_for=10 * DAY)
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        f'[server]\nport = 0\ntls = "tls"\ncert = "{files.cert}"\nkey = "{files.key}"\n'
    )
    frontend = Frontend(state, config_dir, trust=ssl.create_default_context(cafile=ca.path))
    thread = frontend.start()
    result = runner.invoke(app, ["serve", *QUIET])
    thread.join(10)

    assert result.exit_code == 0, result.output
    assert [type(e) for e in frontend.events] == [Welcome, Catalog, Snapshot]
    assert str(frontend.run_file["url"]).startswith("wss://127.0.0.1:")
    assert result.stderr.startswith("Warning: The server certificate expires soon.")


def test_serve_over_tls_without_a_key_names_it(tmp_path: Path) -> None:
    files = make_server(make_ca(tmp_path), tmp_path)
    result = runner.invoke(app, ["serve", *QUIET, "--tls", "tls", "--cert", str(files.cert)])
    assert result.exit_code == 2
    assert "needs key" in result.stderr and "--key" in result.stderr


def test_serve_refuses_a_key_others_can_read_with_the_chmod_that_fixes_it(tmp_path: Path) -> None:
    files = make_server(make_ca(tmp_path), tmp_path)
    files.key.chmod(0o644)
    result = runner.invoke(app, ["serve", *QUIET, *_tls_flags(files)])
    assert result.exit_code == 2
    assert f"chmod 600 {files.key}" in result.stderr


def test_serve_refuses_a_key_that_isnt_the_certificates(tmp_path: Path) -> None:
    files = make_server(make_ca(tmp_path), tmp_path)
    mismatched = ServerFiles(files.cert, other_key(tmp_path))
    result = runner.invoke(app, ["serve", *QUIET, *_tls_flags(mismatched)])
    assert result.exit_code == 2
    assert result.stderr.startswith("The certificate and key don't match.")


def test_serve_refuses_an_expired_certificate_naming_its_dates(tmp_path: Path) -> None:
    files = make_server(make_ca(tmp_path), tmp_path, valid_from=-10 * DAY, valid_for=5 * DAY)
    result = runner.invoke(app, ["serve", *QUIET, *_tls_flags(files)])
    assert result.exit_code == 2
    assert result.stderr.startswith("The server certificate has expired.")
    assert re.search(r"valid from \d{4}-\d\d-\d\d .* to \d{4}-", result.stderr)


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
    serve._on_signals(stopping)
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
