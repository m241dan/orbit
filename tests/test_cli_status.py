"""``orbit status``: what a running control plane is doing, printed once, and how it exits."""

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result
from websockets.asyncio.server import ServerConnection, serve

from orbit import __version__
from orbit.agent import ControlPlane, RunFile
from orbit.agent.domain.usage_totals import UsageTotalsData, usage_totals
from orbit.cli import app
from orbit.cli.status import NOT_RUNNING, _reported, _Unreachable
from orbit.client import HandshakeRefused, messages
from orbit.protocol import (
    CONTROL_PLANE_STREAM,
    AddTask,
    AgentRef,
    AgentsChanged,
    Catalog,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Connected,
    Ended,
    ErrorOccurred,
    Event,
    EventEnvelope,
    HandshakeFailed,
    Hello,
    LinkState,
    ProtocolError,
    Reconnecting,
    RunTasks,
    Stopped,
    TokenRefused,
    TurnEndReason,
    Usage,
    UsageTotal,
    UsageTotals,
    Watch,
    Welcome,
)
from tests.agent_fakes import Call, FakeLedger, board_of
from tests.connection_helpers import joined, say
from tests.plane_fakes import agent_workspace, idle
from tests.server_fakes import TASK, URL, serving_plane

runner = CliRunner()

TODAY = Usage(input_tokens=1000, output_tokens=200, cost_usd=0.25)
LIFETIME = Usage(input_tokens=5000, output_tokens=1000, cost_usd=1.5)
"""What the turns in the workspace served cost today, and over its lifetime."""


class SpentLedger(FakeLedger):
    """A ledger whose totals give :data:`TODAY` and :data:`LIFETIME` spent in the workspace
    ``a``."""

    async def totals(self, today_start: datetime) -> UsageTotalsData:
        return usage_totals({"a": TODAY}, {("a", "a-1"): LIFETIME}, {})


@dataclass(frozen=True)
class Busy:
    """A control plane served at ``url``, its coder running the task ``task_id``."""

    url: str
    task_id: str


@asynccontextmanager
async def busy_plane() -> AsyncIterator[Busy]:
    """A control plane serving the workspace ``a``, at ``/src/a``, its open session running a
    task whose coder waits until the end, with :data:`TODAY` spent."""
    gate, started = asyncio.Event(), asyncio.Event()

    async def waits(call: Call) -> TurnEndReason:
        started.set()
        await gate.wait()
        await call("give_up", reason="Done waiting.")
        return "completed"

    host, _ = agent_workspace("a", waits)
    plane = ControlPlane([host], ledger=SpentLedger())
    async with serving_plane(plane, plane.close) as (url, _):
        connection = await joined(plane, host=host)
        await say(connection, "add", AddTask(task=TASK))
        await say(connection, "run", RunTasks())
        await asyncio.wait_for(started.wait(), 5)
        try:
            yield Busy(url, board_of(host).tasks[0].id)
        finally:
            gate.set()
            await asyncio.wait_for(idle(connection), 5)


async def status_of(*args: str) -> Result:
    """What ``orbit status`` with ``args`` does, run on a thread of its own, as it runs an event
    loop of its own while the test's serves."""
    return await asyncio.to_thread(lambda: runner.invoke(app, ["status", *args]))


@pytest.mark.usefixtures("token_file")
async def test_status_prints_the_workspace_its_active_session_the_agent_and_todays_usage() -> None:
    async with busy_plane() as busy:
        result = await status_of("--connect", busy.url)

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == f"orbit serve {__version__} at {busy.url}"
    assert "  a  /src/a" in lines
    assert any(line.endswith(" · active · running") for line in lines)
    assert f"  a · coder · task {busy.task_id}" in lines
    assert "  today     1,200 tokens · $0.25" in lines
    assert "  lifetime  6,000 tokens · $1.50" in lines


@pytest.mark.usefixtures("token_file")
async def test_status_json_parses_with_the_same_values() -> None:
    async with busy_plane() as busy:
        result = await status_of("--connect", busy.url, "--json")

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert (parsed["url"], parsed["server_version"]) == (busy.url, __version__)
    (workspace,) = parsed["workspaces"]
    assert (workspace["label"], workspace["path"]) == ("a", "/src/a")
    (session,) = workspace["sessions"]
    assert (session["id"], session["active"], session["state"]) == ("a-1", True, "running")
    (agent,) = parsed["agents"]
    assert (agent["workspace"], agent["role"], agent["task_id"]) == ("a", "coder", busy.task_id)
    assert parsed["usage"]["today"] == {"tokens": 1200, "cost_usd": 0.25, "credits": 0.0}


@pytest.mark.usefixtures("token_file")
async def test_status_finds_the_orbit_serve_running_here(state: Path) -> None:
    async with busy_plane() as busy:
        RunFile(state).write(busy.url, os.getpid())
        result = await status_of()

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(f"orbit serve {__version__} at {busy.url}\n")


async def test_status_asks_the_remote_the_user_config_names(token_file: Path) -> None:
    async with busy_plane() as busy:
        config = f'[remotes.work]\nurl = "{busy.url}"\ntoken_file = "token"\n'
        (token_file.parent / "config.toml").write_text(config)
        result = await status_of("--remote", "work")

    assert result.exit_code == 0, result.output
    assert f"  a · coder · task {busy.task_id}" in result.stdout.splitlines()


def test_with_no_orbit_serve_running_it_says_so_and_exits_1() -> None:
    result = runner.invoke(app, ["status"])

    assert result.exit_code == 1
    assert result.stderr == f"{NOT_RUNNING}\n"
    assert result.stdout == ""


async def test_a_token_the_control_plane_refuses_exits_2_saying_so(tmp_path: Path) -> None:
    wrong = tmp_path / "wrong"
    wrong.write_text("not-the-token\n")
    async with busy_plane() as busy:
        result = await status_of("--connect", busy.url, "--token-file", str(wrong))

    assert result.exit_code == 2
    assert result.stderr == messages.TOKEN_REFUSED.format(path=wrong) + "\n"


async def test_a_token_the_orbit_serve_found_refuses_exits_2_saying_so(
    state: Path, config_dir: Path
) -> None:
    config_dir.mkdir()
    (config_dir / "token").write_text("not-the-token\n")
    async with busy_plane() as busy:
        RunFile(state).write(busy.url, os.getpid())
        result = await status_of()

    assert result.exit_code == 2
    assert result.stderr == messages.TOKEN_REFUSED.format(path=config_dir / "token") + "\n"


@pytest.mark.usefixtures("token_file")
def test_a_handshake_the_orbit_serve_found_refuses_exits_2_saying_why(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuses(*args: object) -> bool:
        raise HandshakeRefused("The server rejected the client certificate.")

    monkeypatch.setattr("orbit.cli.chat.accepts", refuses)
    RunFile(state).write(URL, os.getpid())

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 2
    assert result.stderr == "The server rejected the client certificate.\n"


@pytest.mark.usefixtures("token_file")
def test_a_control_plane_that_cant_be_reached_exits_2() -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        url = f"ws://127.0.0.1:{unused.getsockname()[1]}/"

    result = runner.invoke(app, ["status", "--connect", url])

    assert result.exit_code == 2
    assert result.stderr == f"Can't reach the control plane at {url}.\n"


@pytest.mark.usefixtures("token_file")
async def test_a_control_plane_that_doesnt_answer_in_time_exits_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def silent(connection: ServerConnection) -> None:
        async for _ in connection:
            pass

    monkeypatch.setattr("orbit.cli.status.STATUS_SECONDS", 0.2)
    async with serve(silent, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{next(iter(server.sockets)).getsockname()[1]}/"
        result = await status_of("--connect", url)

    assert result.exit_code == 2
    assert result.stderr == f"The control plane at {url} didn't answer within 0.2 seconds.\n"


@pytest.mark.usefixtures("token_file")
def test_a_url_other_than_ws_or_wss_exits_2() -> None:
    result = runner.invoke(app, ["status", "--connect", "http://example.com/"])

    assert result.exit_code == 2
    assert result.stderr == "--connect takes a wss:// or ws:// URL, not http://example.com/.\n"


def test_state_dir_cant_go_with_connect() -> None:
    result = runner.invoke(app, ["status", "--connect", URL, "--state-dir", "/tmp/s"])

    assert result.exit_code == 2
    assert result.stderr.startswith("--state-dir says where a control plane run here")


type Step = Event | LinkState | ProtocolError | None
"""What a scripted control plane does next: send an event, move the link to a state, send what
can't be read as an event, or end the events."""


class ScriptedSession:
    """A session with a control plane that plays ``script`` once a ``Watch`` is sent, noting
    the commands ``sent`` and whether it was ``closed``."""

    def __init__(self, *script: Step) -> None:
        self.script = script
        self.sent: list[Command] = []
        self.closed = False
        self._watchers: list[Callable[[LinkState], None]] = []
        self._events: asyncio.Queue[EventEnvelope | ProtocolError | None] = asyncio.Queue()

    def watch(self, changed: Callable[[LinkState], None]) -> None:
        self._watchers.append(changed)

    async def send(self, envelope: CommandEnvelope) -> None:
        self.sent.append(envelope.payload)
        if isinstance(envelope.payload, Watch):
            for step in self.script:
                self._play(step)

    async def events(self) -> AsyncIterator[EventEnvelope]:
        while (envelope := await self._events.get()) is not None:
            if isinstance(envelope, ProtocolError):
                raise envelope
            yield envelope

    async def close(self) -> None:
        self.closed = True

    def _play(self, step: Step) -> None:
        if step is None or isinstance(step, ProtocolError):
            self._events.put_nowait(step)
        elif isinstance(step, LinkState):
            for changed in self._watchers:
                changed(step)
        else:
            self._events.put_nowait(EventEnvelope(seq=1, payload=step))


WELCOME = Welcome(command_id="status", server_version="9.9.9")
CATALOG = Catalog(workspaces=(), agents=(), triggers=(), lanes=())
CODER = AgentRef(workspace="w", session="s", agent_id="agent-1", role="coder", task_id="t1")
AGENTS = AgentsChanged(agents=(CODER,))
TOTALS = UsageTotals(workspaces=(), total=UsageTotal(today=TODAY, lifetime=LIFETIME))
DISK_ERROR = ErrorOccurred(code="internal", message="disk I/O error")


async def test_the_report_says_hello_watches_the_control_plane_and_closes_once_answered() -> None:
    session = ScriptedSession(
        Connected(URL), WELCOME, CATALOG, CommandAccepted(command_id="watch"), AGENTS, TOTALS
    )

    report = await _reported(session, URL, 5)

    assert (report.url, report.server_version) == (URL, "9.9.9")
    assert [agent.agent_id for agent in report.agents] == ["agent-1"]
    assert report.usage.today.tokens == 1200
    hello, watch = session.sent
    assert isinstance(hello, Hello) and hello.client == f"orbit-status {__version__}"
    assert watch == Watch(streams=(CONTROL_PLANE_STREAM,))
    assert session.closed


async def test_the_latest_of_each_answer_is_reported() -> None:
    later = AgentsChanged(agents=())
    session = ScriptedSession(WELCOME, AGENTS, later, CATALOG, TOTALS)

    report = await _reported(session, URL, 5)

    assert report.agents == ()


@pytest.mark.parametrize(
    ("step", "message"),
    [
        (
            TokenRefused("The control plane refused the token."),
            "The control plane refused the token.",
        ),
        (HandshakeFailed(URL, "The server isn't speaking TLS."), "The server isn't speaking TLS."),
        (
            Ended(URL, "Incompatible"),
            f"The connection to the control plane at {URL} ended: Incompatible.",
        ),
        (Ended(URL, ""), f"The connection to the control plane at {URL} ended."),
        (Stopped(URL), f"The control plane at {URL} is shutting down."),
        (Reconnecting(URL, 0.0), f"Can't reach the control plane at {URL}."),
        (
            CommandRefused(command_id="watch", code="unknown", message="No stream named that."),
            "No stream named that.",
        ),
        (None, f"The connection to the control plane at {URL} closed before it answered."),
    ],
)
async def test_a_link_that_fails_before_every_answer_comes_is_unreachable(
    step: Step, message: str
) -> None:
    session = ScriptedSession(WELCOME, CATALOG, step)

    with pytest.raises(_Unreachable) as raised:
        await _reported(session, URL, 5)

    assert str(raised.value) == message
    assert session.closed


async def test_an_error_in_place_of_the_usage_totals_is_unreachable_saying_why() -> None:
    session = ScriptedSession(WELCOME, CATALOG, AGENTS, DISK_ERROR)

    with pytest.raises(_Unreachable) as raised:
        await _reported(session, URL, 5)

    assert str(raised.value) == "The control plane couldn't read its usage totals: disk I/O error"
    assert session.closed


@pytest.mark.parametrize(
    "script",
    [
        (WELCOME, CATALOG, DISK_ERROR, AGENTS, TOTALS),
        (WELCOME, AGENTS, TOTALS, DISK_ERROR, CATALOG),
    ],
)
async def test_an_error_at_any_other_time_is_ignored(script: tuple[Step, ...]) -> None:
    session = ScriptedSession(*script)

    report = await _reported(session, URL, 5)

    assert report.usage.today.tokens == 1200


async def test_an_event_that_cant_be_read_ends_the_events_before_every_answer_comes() -> None:
    session = ScriptedSession(WELCOME, ProtocolError("Not JSON."), CATALOG)

    with pytest.raises(_Unreachable) as raised:
        await _reported(session, URL, 5)

    assert str(raised.value) == (
        f"The connection to the control plane at {URL} closed before it answered."
    )


async def test_a_control_plane_that_doesnt_answer_in_time_is_unreachable() -> None:
    session = ScriptedSession(WELCOME, CATALOG, AGENTS)

    with pytest.raises(_Unreachable) as raised:
        await _reported(session, URL, 0.05)

    assert str(raised.value) == f"The control plane at {URL} didn't answer within 0.05 seconds."
    assert session.closed
