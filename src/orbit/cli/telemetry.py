"""Starting the telemetry the user config asks for, and stopping it on exit."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer

from orbit.agent import ConfigError, read_user_config
from orbit.cli.errors import _configured, _exit_on
from orbit.telemetry import (
    HeadersFileError,
    RunningTelemetry,
    TelemetryConfigError,
    TelemetryNotInstalled,
    TelemetryRun,
    TelemetrySettings,
    load_telemetry,
    start_telemetry,
)


def _telemetry_settings(config_dir: Path) -> TelemetrySettings:
    """The telemetry settings in the user config in ``config_dir``.

    Exits with status 2 if the file is invalid.
    """
    return _configured(lambda: read_user_config(config_dir, _load, TelemetrySettings()))


def _refuse_console(config_dir: Path) -> None:
    """Exit with status 2 if the user config in ``config_dir`` sends spans to the console,
    which the TUI uses, or is invalid."""
    if _telemetry_settings(config_dir).exporter != "console":
        return
    typer.echo(
        "The console exporter writes to the terminal, which the TUI uses; use it with orbit "
        'serve, or set exporter = "file".',
        err=True,
    )
    raise typer.Exit(2)


def _load(data: dict[str, Any]) -> TelemetrySettings:
    """The telemetry settings in ``data``; raises :class:`ConfigError` if they're invalid."""
    try:
        return load_telemetry(data)
    except TelemetryConfigError as exc:
        raise ConfigError(str(exc)) from None


@contextmanager
def _telemetry(run: TelemetryRun) -> Iterator[RunningTelemetry]:
    """Run the block with the telemetry the user config asks for, for orbit running as ``run``
    says, then flush and stop it.

    Exits with status 2 if the config is invalid, the headers file can't be used, or telemetry
    is configured without the extra that installs it.
    """
    settings = _telemetry_settings(run.config_dir)
    with _exit_on(HeadersFileError, TelemetryNotInstalled):
        telemetry = start_telemetry(settings, run)
    try:
        yield telemetry
    finally:
        telemetry.close()
