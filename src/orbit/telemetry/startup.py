"""Starting the telemetry the settings ask for: none at all, importing nothing of
OpenTelemetry, or the SDK's, which needs the extra that installs it."""

from typing import Protocol

from orbit.telemetry.headers import read_headers
from orbit.telemetry.run import FLUSH_SECONDS, TelemetryRun
from orbit.telemetry.settings import TelemetrySettings
from orbit.tracing import Gauges, NoGauges, NoTelemetry, Telemetry

_NO_GAUGES = NoGauges()


class TelemetryNotInstalled(Exception):
    """Telemetry is configured, but the extra that installs it isn't; the message names it."""


class RunningTelemetry(Telemetry, Protocol):
    """Telemetry started, with the :attr:`gauges` it reports, which ends with :meth:`close`."""

    @property
    def gauges(self) -> Gauges:
        """What the live gauges are registered with."""
        ...

    def close(self) -> None:
        """Export what's left, waiting a short while at most, and stop."""
        ...


class Off(NoTelemetry):
    """Telemetry turned off: it records nothing and reports no gauges, so closing it has
    nothing to do."""

    @property
    def gauges(self) -> NoGauges:
        """Gauges that report nothing."""
        return _NO_GAUGES

    def close(self) -> None:
        """Do nothing: nothing was recorded."""


def start_telemetry(
    settings: TelemetrySettings, run: TelemetryRun, flush_seconds: float = FLUSH_SECONDS
) -> RunningTelemetry:
    """The telemetry ``settings`` ask for, for orbit running as ``run`` says, flushing for at
    most ``flush_seconds`` on exit; with no exporter, :class:`Off`, importing nothing of
    OpenTelemetry.

    Raises :class:`HeadersFileError` if the headers file can't be used, and
    :class:`TelemetryNotInstalled` if OpenTelemetry can't be imported.
    """
    if settings.exporter == "none":
        return Off()
    path = settings.headers_path(run.config_dir) if settings.exporter == "otlp" else None
    headers = read_headers(path) if path else {}
    try:
        from orbit.telemetry.exporters import started

        return started(settings, run, headers, flush_seconds)
    except ImportError:
        raise TelemetryNotInstalled(
            f"Telemetry is configured but not installed: pip install 'orbit[{settings.extra}]'"
        ) from None
