"""The telemetry ``orbit chat`` starts from the user config, hands its backend and stops."""

import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from orbit.agent import WorkspaceHost
from orbit.cli import app
from orbit.cli.telemetry import _quiet_logging
from orbit.telemetry import Off
from orbit.telemetry.otel import OtelTelemetry
from tests.cli_fakes import LocalApp, StartedTelemetry

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
    LocalApp.during = None
    LocalApp.halted = None
    return built


def _configure(config_dir: Path, table: str) -> Path:
    """The user config in ``config_dir``, holding ``table`` as its ``[telemetry]``."""
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "config.toml"
    path.write_text(f"[telemetry]\n{table}\n")
    return path


def test_without_telemetry_configured_chat_traces_nothing(hosts: list[WorkspaceHost]) -> None:
    assert runner.invoke(app, ["chat"]).exit_code == 0

    [host] = hosts
    assert isinstance(host.telemetry, Off)


def test_chat_hands_its_backend_the_telemetry_it_starts_and_closes_it_on_exit(
    hosts: list[WorkspaceHost],
    started_telemetry: list[StartedTelemetry],
    config_dir: Path,
    state: Path,
) -> None:
    _configure(config_dir, 'exporter = "console"')

    assert runner.invoke(app, ["chat"]).exit_code == 0

    [telemetry] = started_telemetry
    assert telemetry.settings.exporter == "console"
    assert telemetry.run.namespace == "orbit-chat"
    assert (telemetry.run.config_dir, telemetry.run.state_dir) == (config_dir, state)
    assert [host.telemetry for host in hosts] == [telemetry]
    assert telemetry.closed


def test_chat_with_the_file_exporter_traces_with_opentelemetry(
    hosts: list[WorkspaceHost], config_dir: Path
) -> None:
    _configure(config_dir, 'exporter = "file"')

    assert runner.invoke(app, ["chat"]).exit_code == 0

    [host] = hosts
    assert isinstance(host.telemetry, OtelTelemetry)


@pytest.mark.parametrize(
    ("line", "key"),
    [('exporter = "jaeger"', "telemetry.exporter"), ("sample = 1", "telemetry.sample")],
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


def _quieted(name: str) -> bool:
    return any(isinstance(h, logging.NullHandler) for h in logging.getLogger(name).handlers)


def test_quiet_logging_sends_orbits_and_opentelemetrys_records_nowhere_for_the_block() -> None:
    with _quiet_logging():
        assert _quieted("orbit") and _quieted("opentelemetry")
    assert not _quieted("orbit") and not _quieted("opentelemetry")


def test_chat_keeps_logging_quiet_while_the_tui_runs(hosts: list[WorkspaceHost]) -> None:
    seen: list[bool] = []

    async def note(host: WorkspaceHost) -> None:
        seen.append(_quieted("orbit"))

    LocalApp.during = note

    assert runner.invoke(app, ["chat"]).exit_code == 0
    assert seen == [True]
    assert not _quieted("orbit")
