"""``orbit serve``: the control plane on its own, served on loopback until stopped."""

import asyncio
import json
import os
import re
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import ClientConnection, connect

from orbit.agent import (
    RunFile,
    TokenFile,
    WorkspaceHost,
    WorkspaceLock,
    WorkspaceOwner,
)
from orbit.agent.domain.server import client_fingerprint
from orbit.cli import app, chat, serve
from orbit.protocol import (
    PROTOCOL_VERSION,
    AddTask,
    AddWorkspace,
    Attach,
    Catalog,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Event,
    Hello,
    Notice,
    OwnershipOffered,
    OwnershipTaken,
    RemoveWorkspace,
    Snapshot,
    TakeOwnership,
    TaskSpec,
    Welcome,
    decode_event,
    encode,
)
from tests.tls_certs import DAY, Ca, ServerFiles, make_ca, make_client, make_server, other_key

runner = CliRunner()
TASK = TaskSpec(prompt="Add a flag.")
QUIET = ["--no-usage", "--no-verify"]
# What a frontend receives saying Hello and attaching, up to its Attach's acceptance.
ATTACHED = [Welcome, Catalog, Snapshot, Notice, CommandAccepted]


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
    ``state``, showing the token in ``config_dir``: it says Hello and attaches as ``greet``
    does, does ``work`` and stops the server with SIGTERM, noting what it saw. It stops the
    server however its work ends, so a failure on its thread fails the test rather than leaving
    the server running."""

    state: Path
    config_dir: Path
    work: Callable[[ClientConnection], list[Event]] = lambda websocket: []
    greet: Callable[[ClientConnection], list[Event]] = lambda websocket: attached(websocket)
    listening: Callable[[Path], dict[str, object]] = lambda state: _run_file(state)
    run_file: dict[str, object] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    close: tuple[int | None, str | None] = (None, None)
    trust: ssl.SSLContext | None = None
    stopped: bool = False  # Whether it sent the server SIGTERM.

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()
        return thread

    def _run(self) -> None:
        try:
            self._talk()
        finally:
            self._stop()

    def _stop(self) -> None:
        """Stop the server, once."""
        if not self.stopped:
            self.stopped = True
            os.kill(os.getpid(), signal.SIGTERM)

    def _talk(self) -> None:
        self.run_file = self.listening(self.state)
        token = (self.config_dir / "token").read_text().strip()
        # A server listening on every address is reached on the one its certificate names.
        url = str(self.run_file["url"]).replace("//0.0.0.0:", "//localhost:")
        headers = {"Authorization": f"Bearer {token}"}
        with connect(url, additional_headers=headers, ssl=self.trust) as ws:
            self.events = self.greet(ws) + self.work(ws)
            self._stop()
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


def welcomed(websocket: ClientConnection) -> list[Event]:
    """Say Hello on ``websocket``; what it received, up to the Catalog."""
    say(websocket, "hello", Hello(client="orbit-tests", protocol_version=PROTOCOL_VERSION))
    return until(websocket, Catalog)


def attached(websocket: ClientConnection) -> list[Event]:
    """Say Hello on ``websocket``, then attach to the active session of the first workspace its
    catalog lists; what it received, up to the Attach's acceptance."""
    greeting = welcomed(websocket)
    catalog = greeting[-1]
    assert isinstance(catalog, Catalog)
    [workspace, *_] = catalog.workspaces
    [active] = [summary for summary in workspace.sessions if summary.active]
    say(websocket, "attach", Attach(workspace=workspace.id, session=active.id))
    return greeting + until(websocket, CommandAccepted)


def closed(websocket: ClientConnection) -> tuple[int | None, str | None]:
    with pytest.raises(ConnectionClosed):
        while True:
            websocket.recv(timeout=10)
    return websocket.close_code, websocket.close_reason


def adds_a_task(websocket: ClientConnection) -> list[Event]:
    say(websocket, "add", AddTask(task=TASK))
    return until(websocket, CommandAccepted)


def served(frontend: Frontend, *args: str, added: tuple[str, ...] = (".",)) -> str:
    """What ``orbit serve`` with ``args`` printed, adding the workspaces at the paths ``added``
    (the current directory, by default), serving ``frontend`` until it stops it."""
    thread = frontend.start()
    adding = [flag for path in added for flag in ("--add", path)]
    result = runner.invoke(app, ["serve", *QUIET, *adding, *args])
    thread.join(10)
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_frontend_failing_on_its_thread_still_stops_serve(state: Path, config_dir: Path) -> None:
    def breaks(websocket: ClientConnection) -> list[Event]:
        raise RuntimeError("the frontend broke")

    frontend = Frontend(state, config_dir, breaks)
    served(frontend, "--port", "0")

    assert frontend.stopped
    assert frontend.close == (None, None)  # It never got as far as the close.


def test_serve_welcomes_a_frontend_with_the_token_until_stopped(
    project: Path, state: Path, config_dir: Path
) -> None:
    frontend = Frontend(state, config_dir)
    output = served(frontend, "--port", "0")

    assert [type(e) for e in frontend.events] == ATTACHED
    url = str(frontend.run_file["url"])
    assert url.startswith("ws://127.0.0.1:") and not url.endswith(":0/")
    assert frontend.run_file["pid"] == os.getpid()
    assert frontend.close == (1001, "orbit serve is shutting down")
    assert not list((state / "run").iterdir())
    token = config_dir / "token"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    lines = output.splitlines()
    assert lines[0] == (
        f"Serving at {url}: {project} as its owner. Frontends show the token in {token}."
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
    output = served(frontend, "--port", "0", added=(str(project),))

    assert [type(e) for e in frontend.events] == ATTACHED
    url = frontend.run_file["url"]
    assert output.splitlines()[0] == (
        f"Serving at {url}: {project} as its owner. Frontends show the token in {config / 'token'}."
    )


def test_serve_stops_if_it_cant_write_its_run_file_naming_it(state: Path) -> None:
    state.mkdir()
    (state / "run").write_text("not a directory\n")
    result = runner.invoke(app, ["serve", *QUIET, "--port", "0"])

    assert result.exit_code == 2
    assert f"Can't write the run file {state / 'run'}" in result.stderr
    assert "Traceback" not in result.output
    assert not [t for t in threading.enumerate() if t.name.startswith("orbit-sqlite")]


def test_serve_stops_if_it_cant_name_its_url_in_the_workspace_lock_naming_it(
    project: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unwritable(lock: WorkspaceLock, url: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(WorkspaceLock, "announce", unwritable)
    result = runner.invoke(app, ["serve", *QUIET, "--add", ".", "--port", "0"])

    lock = project / ".orbit" / "workspace.lock"
    assert result.exit_code == 2
    assert f"Can't write the workspace lock {lock}: disk full" in result.stderr
    assert "Traceback" not in result.output
    assert not list((state / "run").glob("*.json"))
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
    monkeypatch: pytest.MonkeyPatch, project: Path, state: Path, config_dir: Path
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
        f"Serving at {url}: {project} read-only, as an orbit chat, pid {os.getpid()} owns it."
        f" Frontends show the token in {token}."
    )


@pytest.fixture
def quick_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Have a read-only serve try the repository's lock every 50ms rather than every 5s."""
    defaults = WorkspaceHost.__init__.__kwdefaults__
    assert defaults is not None
    monkeypatch.setitem(defaults, "lock_retry", 0.05)


@pytest.mark.usefixtures("quick_retries")
def test_a_read_only_workspace_taken_over_names_the_servers_url_in_its_lock(
    project: Path, state: Path, config_dir: Path
) -> None:
    owner = WorkspaceLock(project / ".orbit")
    owner.take()
    owners: list[WorkspaceOwner] = []

    def takes_over(websocket: ClientConnection) -> list[Event]:
        owner.close()
        events = until(websocket, OwnershipOffered)
        say(websocket, "take", TakeOwnership(take=True))
        events += until(websocket, OwnershipTaken)
        owners.append(WorkspaceLock(project / ".orbit").owner())
        return events + adds_a_task(websocket)

    frontend = Frontend(state, config_dir, takes_over)
    served(frontend, "--port", "0")

    assert owners == [WorkspaceOwner(pid=os.getpid(), url=str(frontend.run_file["url"]))]
    assert isinstance(frontend.events[-1], CommandAccepted)


def _workspace_dirs(tmp_path: Path, *names: str) -> list[Path]:
    """New directories in ``tmp_path`` named ``names``, to serve as workspaces."""
    paths = [tmp_path / name for name in names]
    for path in paths:
        path.mkdir()
    return paths


def _ready(url: object) -> dict[str, object]:
    """What ``/readyz`` answers on the server at the WebSocket ``url``."""
    health = str(url).replace("ws://", "http://") + "readyz"
    with urllib.request.urlopen(health, timeout=10) as response:
        answer: dict[str, object] = json.loads(response.read())
        return answer


def _checking_readiness(
    frontend: Frontend, readiness: list[dict[str, object]]
) -> Callable[[ClientConnection], list[Event]]:
    """What notes in ``readiness`` what ``/readyz`` answers on the server ``frontend`` is
    connected to."""

    def checks(websocket: ClientConnection) -> list[Event]:
        readiness.append(_ready(frontend.run_file["url"]))
        return []

    return checks


def test_serve_with_two_workspaces_added_lists_both_and_is_ready(
    state: Path, config_dir: Path, tmp_path: Path
) -> None:
    first, second = _workspace_dirs(tmp_path, "a", "b")
    readiness: list[dict[str, object]] = []
    frontend = Frontend(state, config_dir)
    frontend.work = _checking_readiness(frontend, readiness)
    output = served(frontend, "--port", "0", added=(str(first), str(second)))

    [catalog] = [e for e in frontend.events if isinstance(e, Catalog)]
    assert [(w.path, w.read_only) for w in catalog.workspaces] == [
        (str(first), False),
        (str(second), False),
    ]
    assert readiness == [{"status": "pass"}]
    assert f": {first} as its owner; {second} as its owner. Frontends" in output


def test_a_workspace_another_orbit_owns_is_read_only_and_serve_warns_it_is(
    state: Path, config_dir: Path, tmp_path: Path
) -> None:
    first, second = _workspace_dirs(tmp_path, "a", "b")
    owner = WorkspaceLock(second / ".orbit")
    owner.take()
    readiness: list[dict[str, object]] = []
    frontend = Frontend(state, config_dir)
    frontend.work = _checking_readiness(frontend, readiness)
    try:
        served(frontend, "--port", "0", added=(str(first), str(second)))
    finally:
        owner.close()

    [catalog] = [e for e in frontend.events if isinstance(e, Catalog)]
    assert [w.read_only for w in catalog.workspaces] == [False, True]
    assert readiness == [{"status": "warn", "output": "read_only"}]


def test_serve_with_no_workspace_flagged_starts_empty_and_says_so(
    state: Path, config_dir: Path
) -> None:
    frontend = Frontend(state, config_dir, greet=welcomed)
    output = served(frontend, "--port", "0", added=())

    [catalog] = [e for e in frontend.events if isinstance(e, Catalog)]
    assert catalog.workspaces == ()
    url = frontend.run_file["url"]
    assert output.splitlines()[0] == (
        f"Serving at {url}: no workspaces yet; add one with orbit serve --add PATH. Frontends"
        f" show the token in {config_dir / 'token'}."
    )


def test_a_second_serve_exits_naming_the_first(state: Path) -> None:
    RunFile(state).write("ws://127.0.0.1:7433/", os.getpid())
    result = runner.invoke(app, ["serve", *QUIET, "--add", ".", "--port", "0"])

    assert result.exit_code == 2
    assert result.stderr == (
        f"orbit serve is already running at ws://127.0.0.1:7433/ (pid {os.getpid()}); use"
        " --state-dir for a separate one.\n"
    )


def test_a_stale_run_file_is_replaced(state: Path, config_dir: Path) -> None:
    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()
    RunFile(state).write("ws://127.0.0.1:1/", process.pid)

    def ours(state: Path) -> dict[str, object]:
        while (found := _run_file(state))["pid"] != os.getpid():
            time.sleep(0.02)
        return found

    frontend = Frontend(state, config_dir, listening=ours)
    served(frontend, "--port", "0")
    assert [type(e) for e in frontend.events] == ATTACHED
    assert not RunFile(state).path.exists()


def test_a_workspace_whose_directory_is_gone_is_not_served(
    state: Path, config_dir: Path, tmp_path: Path
) -> None:
    [gone] = _workspace_dirs(tmp_path, "gone")
    served(Frontend(state, config_dir), "--port", "0", added=(str(gone),))
    shutil.rmtree(gone)

    output = served(Frontend(state, config_dir), "--port", "0")
    assert f"Warning: The workspace {gone} is gone, so it isn't served." in output
    assert str(gone) not in output.splitlines()[-4]


def test_adding_a_missing_workspace_exits_naming_it(tmp_path: Path) -> None:
    result = runner.invoke(app, ["serve", *QUIET, "--add", str(tmp_path / "missing")])
    assert result.exit_code == 2
    assert f"Workspace {tmp_path / 'missing'} doesn't exist." in result.stderr


def _changes(websocket: ClientConnection, command_id: str, command: Command) -> list[Event]:
    """Send ``command`` on ``websocket``; what it received, up to the command's outcome."""
    say(websocket, command_id, command)
    events: list[Event] = []
    while not events or not isinstance(events[-1], CommandAccepted | CommandRefused):
        events += until(websocket, CommandAccepted | CommandRefused)  # type: ignore[arg-type]
    return events


def _lock_free(root: Path) -> bool:
    """Whether no orbit holds the workspace lock on the repository at ``root``."""
    lock = WorkspaceLock(root / ".orbit")
    owner = lock.take()
    lock.close()
    return owner is None


def _catalogs(events: list[Event]) -> list[Catalog]:
    return [event for event in events if isinstance(event, Catalog)]


def test_a_frontend_adds_and_removes_a_workspace_which_keeps_its_sessions(
    state: Path, config_dir: Path, tmp_path: Path
) -> None:
    [other] = _workspace_dirs(tmp_path, "other")
    seen: dict[str, object] = {}

    def adds_and_removes(websocket: ClientConnection) -> list[Event]:
        [added] = _catalogs(_changes(websocket, "add", AddWorkspace(path=str(other))))
        seen["held"] = WorkspaceLock(other / ".orbit").owner()
        seen["again"] = _changes(websocket, "again", AddWorkspace(path=str(other)))
        entry = added.workspaces[-1]
        [session] = entry.sessions
        say(websocket, "attach", Attach(workspace=entry.id, session=session.id))
        until(websocket, CommandAccepted)
        adds_a_task(websocket)
        [removed] = _catalogs(_changes(websocket, "remove", RemoveWorkspace(workspace=entry.id)))
        seen["free"] = _lock_free(other)
        [readded] = _catalogs(_changes(websocket, "readd", AddWorkspace(path=str(other))))
        seen["catalogs"] = [added, removed, readded]
        return []

    frontend = Frontend(state, config_dir, adds_and_removes, greet=welcomed)
    served(frontend, "--port", "0")

    assert seen["held"] == WorkspaceOwner(pid=os.getpid(), url=str(frontend.run_file["url"]))
    assert seen["again"] == [CommandAccepted(command_id="again")]
    assert seen["free"] is True
    added, removed, readded = cast(list[Catalog], seen["catalogs"])
    project = str(Path.cwd())
    listed = [[w.path for w in catalog.workspaces] for catalog in (added, removed, readded)]
    assert listed == [[project, str(other)], [project], [project, str(other)]]
    first, again = added.workspaces[-1], readded.workspaces[-1]
    assert again.id == first.id
    assert [s.id for s in again.sessions] == [s.id for s in first.sessions]
    assert _lock_free(other)


def test_a_frontend_adding_a_relative_path_is_refused(state: Path, config_dir: Path) -> None:
    def adds(websocket: ClientConnection) -> list[Event]:
        return _changes(websocket, "add", AddWorkspace(path="relative"))

    frontend = Frontend(state, config_dir, adds)
    served(frontend, "--port", "0")

    [refusal] = [e for e in frontend.events if isinstance(e, CommandRefused)]
    assert refusal.code == "invalid"


def test_serve_takes_its_port_from_the_config_file(state: Path, config_dir: Path) -> None:
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("[server]\nport = 0\n")
    frontend = Frontend(state, config_dir)
    served(frontend)
    assert not str(frontend.run_file["url"]).endswith(":7433/")


def test_serve_with_no_flags_serves_the_workspaces_added_before_resuming_their_sessions(
    project: Path, state: Path, config_dir: Path
) -> None:
    served(Frontend(state, config_dir, adds_a_task), "--port", "0")

    resumed = Frontend(state, config_dir)
    output = served(resumed, "--port", "0", added=())

    [snapshot] = [e for e in resumed.events if isinstance(e, Snapshot)]
    assert [t.task for t in snapshot.tasks] == [TASK]
    assert f": {project} as its owner." in output.splitlines()[0]


def test_a_chat_discovers_the_orbit_serve_running(
    project: Path, state: Path, config_dir: Path
) -> None:
    found: list[chat.Discovered | None] = []

    def discovers(websocket: ClientConnection) -> list[Event]:
        found.append(chat.discover_control_plane(state, config_dir / "token", config_dir))
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

    assert [type(e) for e in frontend.events] == ATTACHED
    assert str(frontend.run_file["url"]).startswith("wss://0.0.0.0:")
    assert "orbit-tests connected from" in output


def _discovering(
    state: Path, config_dir: Path, found: list[chat.Discovered | None]
) -> Callable[[ClientConnection], list[Event]]:
    """What has a chat discover the control plane running, noting what it found in
    ``found``."""

    def discovers(websocket: ClientConnection) -> list[Event]:
        found.append(chat.discover_control_plane(state, config_dir / "token", config_dir))
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
    frontend = Frontend(state, config_dir, _discovering(state, config_dir, found), trust=trust)
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
    assert f"Frontends show a client certificate {ca.path} signed." in output


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
    discovers = _discovering(state, config_dir, found)
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
    result = runner.invoke(app, ["serve", *QUIET, "--add", "."])
    thread.join(10)

    assert result.exit_code == 0, result.output
    assert [type(e) for e in frontend.events] == ATTACHED
    assert str(frontend.run_file["url"]).startswith("wss://127.0.0.1:")
    assert result.stderr.startswith("Warning: The server certificate expires soon.")


def test_serve_takes_relative_tls_flags_from_where_it_was_started(
    monkeypatch: pytest.MonkeyPatch, project: Path, state: Path, config_dir: Path, tmp_path: Path
) -> None:
    certs = tmp_path / "certs"
    certs.mkdir()
    ca = make_ca(certs)
    make_server(ca, certs)
    monkeypatch.chdir(certs)
    frontend = Frontend(state, config_dir, trust=ssl.create_default_context(cafile=ca.path))
    flags = ["--tls", "tls", "--cert", "server.pem", "--key", "server-key.pem", "--port", "0"]
    served(frontend, *flags, added=(str(project),))

    assert [type(e) for e in frontend.events] == ATTACHED
    assert str(frontend.run_file["url"]).startswith("wss://127.0.0.1:")


def test_serve_takes_tls_files_in_the_config_file_from_home_and_the_config_directory(
    monkeypatch: pytest.MonkeyPatch, state: Path, config_dir: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    (config_dir / "tls").mkdir(parents=True)
    home.mkdir()
    ca = make_ca(tmp_path)
    files = make_server(ca, config_dir / "tls")
    files.key.rename(home / "server-key.pem")
    (config_dir / "config.toml").write_text(
        '[server]\nport = 0\ntls = "tls"\ncert = "tls/server.pem"\nkey = "~/server-key.pem"\n'
    )
    frontend = Frontend(state, config_dir, trust=ssl.create_default_context(cafile=ca.path))
    served(frontend)

    assert [type(e) for e in frontend.events] == ATTACHED


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


@pytest.fixture
def no_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project: Path) -> Path:
    """The project made a git repository, with git configured with no identity."""
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.setenv("EMAIL", "")
    for name in ("AUTHOR", "COMMITTER"):
        for part in ("NAME", "EMAIL"):
            monkeypatch.delenv(f"GIT_{name}_{part}", raising=False)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
    return project


def test_serve_refuses_a_repository_git_has_no_identity_for(no_identity: Path) -> None:
    result = runner.invoke(app, ["serve", *QUIET, "--add", ".", "--port", "0"])
    assert result.exit_code == 2
    assert result.stderr == (
        f"Git has no identity for commits in {no_identity}: set user.name and user.email"
        ' (git config --global user.name "…"), or mount your ~/.gitconfig into the'
        " container.\n"
    )


def test_serve_takes_the_identity_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, no_identity: Path, state: Path, config_dir: Path
) -> None:
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "author@example.com")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "committer@example.com")
    output = served(Frontend(state, config_dir), "--port", "0")
    assert f": {no_identity} as its owner." in output


def test_serve_takes_the_identity_from_the_repositorys_config(
    no_identity: Path, state: Path, config_dir: Path
) -> None:
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=no_identity, check=True)
    output = served(Frontend(state, config_dir), "--port", "0")
    assert f": {no_identity} as its owner." in output


def test_serve_without_a_worktree_needs_no_identity(
    no_identity: Path, state: Path, config_dir: Path
) -> None:
    output = served(Frontend(state, config_dir), "--port", "0", "--no-worktree")
    assert f": {no_identity} as its owner." in output


def test_serve_refuses_an_invalid_server_setting(config_dir: Path) -> None:
    config_dir.mkdir()
    path = config_dir / "config.toml"
    path.write_text("[server]\nport = 'high'\n")
    result = runner.invoke(app, ["serve", *QUIET, "--add", "."])
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
            attached(other)
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
