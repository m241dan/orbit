"""How orbit is running, as its telemetry describes it and finds its files."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

FLUSH_SECONDS = 5.0
"""The longest orbit waits on exit for the spans not yet exported."""


@dataclass(frozen=True)
class TelemetryRun:
    """The orbit telemetry is for: its ``namespace``, ``orbit-serve`` or ``orbit-chat``, its
    ``config_dir`` and ``state_dir``, and the ``environ`` it runs in."""

    namespace: str
    config_dir: Path
    state_dir: Path
    environ: Mapping[str, str]
