"""The tracing port over the OpenTelemetry SDK, wired in by the CLI alone.

Importing it imports nothing of OpenTelemetry: :func:`start_telemetry` does, only when the
settings turn telemetry on.
"""

from orbit.telemetry.headers import HeadersFileError, parse_headers, read_headers
from orbit.telemetry.run import FLUSH_SECONDS, TelemetryRun
from orbit.telemetry.settings import (
    OtlpTarget,
    TelemetryConfigError,
    TelemetrySettings,
    env_headers,
    load_telemetry,
    otlp_target,
)
from orbit.telemetry.startup import (
    Off,
    RunningTelemetry,
    TelemetryNotInstalled,
    start_telemetry,
)

__all__ = [
    "FLUSH_SECONDS",
    "HeadersFileError",
    "Off",
    "OtlpTarget",
    "RunningTelemetry",
    "TelemetryConfigError",
    "TelemetryNotInstalled",
    "TelemetryRun",
    "TelemetrySettings",
    "env_headers",
    "load_telemetry",
    "otlp_target",
    "parse_headers",
    "read_headers",
    "start_telemetry",
]
