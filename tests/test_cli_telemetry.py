"""The telemetry ``orbit chat`` starts from the user config, hands its backend and its TUI,
or a TUI joining a control plane its own, and stops."""

import io
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from orbit.agent import ClaudeCliRunner, ControlPlane, WorkspaceHost
from orbit.cli import app
from orbit.protocol import RunTasks
from orbit.telemetry import Off
from orbit.telemetry.gauges import OtelGauges
from orbit.telemetry.otel import OtelTelemetry
from orbit.tracing import NoGauges
from orbit.tui.view.actions import action, carried
from tests.cli_fakes import AGENT_LAUNCH, HELLO, FakeApp, LocalApp, StartedTelemetry
from tests.server_fakes import URL

runner = CliRunner()


@pytest.fixture(autouse=True)
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """The workspace chat runs in, the current directory, put back afterwards."""
    monkeypatch.chdir(Path.cwd())
    path = tmp_path / "project"
    path.mkdir()
    monkeypatch.chdir(path)
    yield path


@pytest.fixture
def hosts(monkeypatch: pytest.MonkeyPatch) -> list[WorkspaceHost]:
    """The backend hosts chat builds, running a :class:`LocalApp` over them."""
    built: list[WorkspaceHost] = []

    def host(*args: Any, **kwargs: Any) -> WorkspaceHost:
        built.append(WorkspaceHost(*args, **kwargs))
        LocalApp.launched.append(built[-1])
        return built[-1]

    monkeypatch.setattr("orbit.cli.chat.OrbitApp", LocalApp)
    monkeypatch.setattr("orbit.cli.backend.WorkspaceHost", host)
    LocalApp.launched = []
    LocalApp.telemetries = []
    LocalApp.during = None
    LocalApp.halted = None
    return built


TELEMETRY_VARIABLES = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_RESOURCE_ATTRIBUTES",
)
"""The variables that have an agent send its telemetry to a collector."""


@pytest.fixture
def untelemetered(monkeypatch: pytest.MonkeyPatch) -> None:
    """An environment for orbit setting none of the variables agents' telemetry takes."""
    for name in (*TELEMETRY_VARIABLES, "OTEL_EXPORTER_OTLP_HEADERS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def planes(monkeypatch: pytest.MonkeyPatch) -> list[ControlPlane]:
    """The control planes chat builds over its hosts."""
    built: list[ControlPlane] = []

    def plane(*args: Any, **kwargs: Any) -> ControlPlane:
        built.append(ControlPlane(*args, **kwargs))
        return built[-1]

    monkeypatch.setattr("orbit.cli.backend.ControlPlane", plane)
    return built


def _configure(config_dir: Path, table: str) -> Path:
    """The user config in ``config_dir``, holding ``table`` as its ``[telemetry]``."""
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "config.toml"
    path.write_text(f"[telemetry]\n{table}\n")
    return path


def test_without_telemetry_configured_chat_traces_nothing(
    hosts: list[WorkspaceHost], planes: list[ControlPlane]
) -> None:
    assert runner.invoke(app, ["chat"]).exit_code == 0

    [host], [plane] = hosts, planes
    assert isinstance(host.telemetry, Off)
    assert isinstance(plane.gauges, NoGauges)


def test_chat_hands_its_backend_and_tui_the_telemetry_it_starts_and_closes_it_on_exit(
    hosts: list[WorkspaceHost],
    planes: list[ControlPlane],
    started_telemetry: list[StartedTelemetry],
    config_dir: Path,
    state: Path,
) -> None:
    _configure(config_dir, 'exporter = "file"')

    assert runner.invoke(app, ["chat"]).exit_code == 0

    [telemetry] = started_telemetry
    assert telemetry.settings.exporter == "file"
    assert telemetry.run.namespace == "orbit-chat"
    assert (telemetry.run.config_dir, telemetry.run.state_dir) == (config_dir, state)
    assert [host.telemetry for host in hosts] == [telemetry]
    assert [plane.telemetry for plane in planes] == [telemetry]
    assert [plane.gauges for plane in planes] == [telemetry.gauges]
    assert LocalApp.telemetries == [telemetry]
    assert telemetry.closed


@pytest.mark.usefixtures("hosts", "started_telemetry", "untelemetered")
def test_with_the_otlp_exporter_chats_agents_send_their_telemetry_without_its_headers(
    runners: list[ClaudeCliRunner], config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer%20orbits-key")
    _configure(config_dir, 'exporter = "otlp"\nendpoint = "http://c:4318"')

    assert runner.invoke(app, ["chat"]).exit_code == 0

    [agents] = runners
    env = agents.env(AGENT_LAUNCH)
    assert {name: env[name] for name in TELEMETRY_VARIABLES} == {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318",
        "OTEL_RESOURCE_ATTRIBUTES": (
            "orbit.task.id=t1,orbit.agent.id=agent-1,orbit.role=coder,orbit.workspace.id=w"
        ),
    }
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env


@pytest.mark.usefixtures("hosts", "started_telemetry", "untelemetered")
def test_with_the_file_exporter_chats_agents_send_no_telemetry(
    runners: list[ClaudeCliRunner], config_dir: Path
) -> None:
    _configure(config_dir, 'exporter = "file"\nendpoint = "http://c:4318"')

    assert runner.invoke(app, ["chat"]).exit_code == 0

    [agents] = runners
    assert not set(TELEMETRY_VARIABLES) & set(agents.env(AGENT_LAUNCH))


@pytest.mark.usefixtures("fakes", "token_file")
def test_a_connected_chat_traces_its_tui_with_the_telemetry_configured_here(
    started_telemetry: list[StartedTelemetry], config_dir: Path, state: Path
) -> None:
    _configure(config_dir, 'exporter = "file"')

    result = runner.invoke(app, ["chat", "--connect", URL])

    assert result.exit_code == 0, result.output
    [telemetry] = started_telemetry
    assert telemetry.settings.exporter == "file"
    assert telemetry.run.namespace == "orbit-chat"
    assert (telemetry.run.config_dir, telemetry.run.state_dir) == (config_dir, state)
    [tui] = FakeApp.built
    assert tui.telemetry is telemetry and tui.ran
    assert telemetry.closed


@pytest.mark.usefixtures("fakes", "token_file")
def test_a_connected_chat_without_telemetry_sends_no_trace_context() -> None:
    assert runner.invoke(app, ["chat", "--connect", URL]).exit_code == 0

    [tui] = FakeApp.built
    assert isinstance(tui.telemetry, Off)
    with action("run_tasks"):
        sent = carried(HELLO.model_copy(update={"payload": RunTasks()}), tui.telemetry)
    assert (sent.traceparent, sent.tracestate) == (None, None)


def test_chat_with_the_file_exporter_traces_with_opentelemetry(
    hosts: list[WorkspaceHost], planes: list[ControlPlane], config_dir: Path
) -> None:
    _configure(config_dir, 'exporter = "file"')

    assert runner.invoke(app, ["chat"]).exit_code == 0

    [host], [plane] = hosts, planes
    assert isinstance(host.telemetry, OtelTelemetry)
    assert isinstance(plane.gauges, OtelGauges)


@pytest.mark.parametrize("flags", [[], ["--local"], ["--connect", "ws://127.0.0.1:1"]])
def test_the_console_exporter_stops_chat_before_the_tui_starts(
    hosts: list[WorkspaceHost],
    started_telemetry: list[StartedTelemetry],
    config_dir: Path,
    flags: list[str],
) -> None:
    _configure(config_dir, 'exporter = "console"')

    result = runner.invoke(app, ["chat", *flags])

    assert result.exit_code == 2
    assert result.stderr == (
        "The console exporter writes over the TUI; use it with orbit serve, or set exporter = "
        '"file" or "otlp".\n'
    )
    assert (hosts, LocalApp.launched, started_telemetry) == ([], [], [])


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ('exporter = "jaeger"', "telemetry.exporter"),
        ("sample = 1", "telemetry.sample"),
        ("metrics_interval_s = 0", "telemetry.metrics_interval_s"),
        ('metrics_interval_s = "x"', "telemetry.metrics_interval_s"),
    ],
)
def test_an_invalid_telemetry_setting_stops_chat_naming_the_file_and_the_key(
    hosts: list[WorkspaceHost], config_dir: Path, line: str, key: str
) -> None:
    path = _configure(config_dir, line)

    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 2
    assert f"Invalid orbit configuration: {path}: {key}: " in result.stderr
    assert hosts == []


def test_a_headers_file_others_can_read_stops_chat_with_the_chmod_hint(
    hosts: list[WorkspaceHost], config_dir: Path
) -> None:
    _configure(config_dir, 'exporter = "otlp"\nheaders_file = "otel-headers"')
    headers = config_dir / "otel-headers"
    headers.write_text("Authorization: Bearer abc\n")
    headers.chmod(0o644)

    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 2
    assert f"run chmod 600 {headers}." in result.stderr
    assert hosts == []


def test_telemetry_configured_without_its_extra_stops_chat_naming_the_extra(
    hosts: list[WorkspaceHost], config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "orbit.telemetry.exporters", None)
    _configure(config_dir, 'exporter = "otlp"\nprotocol = "grpc"')

    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 2
    assert result.stderr == (
        "Telemetry is configured but not installed: pip install 'orbit[telemetry-grpc]'\n"
    )
    assert hosts == []


def _stderr_so_far() -> bytes:
    """What the CLI runner's stderr has been written so far."""
    sys.stderr.flush()
    buffer = sys.stderr.buffer
    assert isinstance(buffer, io.BytesIO)
    return buffer.getvalue()


def test_chat_prints_the_warnings_logged_while_the_tui_ran_only_after_it_exits(
    hosts: list[WorkspaceHost], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without pytest's handlers, an uncaptured warning goes to stderr, as it would in a run.
    monkeypatch.setattr(logging.root, "handlers", [])
    during: list[bytes] = []

    async def warn(host: WorkspaceHost) -> None:
        for count in (1, 2):
            logging.getLogger("orbit.telemetry.otel").warning("Dropped %d spans.", count)
        logging.getLogger("opentelemetry.sdk").error("Export failed.")
        during.append(_stderr_so_far())

    LocalApp.during = warn
    LocalApp.halted = "Halted."

    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    assert during == [b""]
    assert result.stderr == "Dropped 1 spans.\nDropped 2 spans.\nExport failed.\nHalted.\n"
