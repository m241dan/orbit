"""The user config's ``[telemetry]``, and where spans and metrics go once the environment has
its say."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Exporter = Literal["none", "otlp", "console", "file"]
"""Where spans and metrics go: nowhere, to a collector over OTLP, to stderr, or to files in the
state directory."""

OtlpProtocol = Literal["http/protobuf", "grpc"]
"""How spans and metrics reach a collector over OTLP."""

DEFAULT_ENDPOINTS: dict[OtlpProtocol, str] = {
    "http/protobuf": "http://localhost:4318",
    "grpc": "http://localhost:4317",
}
"""The collector's base URL OTLP sends to by default, by protocol."""

Signal = Literal["traces", "metrics"]
"""What OTLP sends: spans or metrics. OTLP over HTTP adds ``/v1/<signal>`` to a collector's base
URL for each, and each has variables of its own, ``OTEL_EXPORTER_OTLP_<SIGNAL>_*``."""


class TelemetryConfigError(ValueError):
    """``[telemetry]`` is invalid; the message says where and why, on one line per problem."""


class TelemetrySettings(BaseModel):
    """The user config's ``[telemetry]``: the ``exporter`` spans and metrics go to, and for OTLP
    the collector's base URL ``endpoint`` (the exporter's default if ``None``), the
    ``protocol``, and the ``headers_file`` holding the headers sent; the files the file exporter
    writes grow to ``file_max_mb`` each, ``file_keep`` of them kept; metrics are exported every
    ``metrics_interval_s`` seconds; ``service_name`` names orbit to the collector."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    exporter: Exporter = "none"
    endpoint: str | None = Field(default=None, min_length=1)
    protocol: OtlpProtocol = "http/protobuf"
    headers_file: str | None = Field(default=None, min_length=1)
    file_max_mb: int = Field(default=50, ge=1)
    file_keep: int = Field(default=5, ge=1)
    metrics_interval_s: int = Field(default=60, ge=1)
    service_name: str = Field(default="orbit", min_length=1)

    @property
    def extra(self) -> str:
        """The extra that installs what these settings export with."""
        return "telemetry-grpc" if self._grpc else "telemetry"

    @property
    def _grpc(self) -> bool:
        return self.exporter == "otlp" and self.protocol == "grpc"

    def headers_path(self, config_dir: Path) -> Path | None:
        """The headers file, ``~`` expanded and a relative one taken from ``config_dir``;
        ``None`` if there's none."""
        if self.headers_file is None:
            return None
        return config_dir / Path(self.headers_file).expanduser()


class _UserConfig(BaseModel):
    """The part of the user config telemetry reads; the rest is left to others."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    telemetry: TelemetrySettings = TelemetrySettings()


def load_telemetry(data: dict[str, Any]) -> TelemetrySettings:
    """The telemetry settings in ``data``, a user config file's contents, over the defaults; its
    tables but ``[telemetry]`` are ignored.

    Raises :class:`TelemetryConfigError` naming the key if ``[telemetry]`` is invalid.
    """
    try:
        return _UserConfig.model_validate(data).telemetry
    except ValidationError as exc:
        problems = (_problem(problem["loc"], problem["msg"]) for problem in exc.errors())
        raise TelemetryConfigError("\n".join(problems)) from None


def _problem(where: tuple[int | str, ...], why: str) -> str:
    """One problem, saying where and why."""
    return f"{'.'.join(str(part) for part in where)}: {why}"


@dataclass(frozen=True)
class OtlpTarget:
    """Where OTLP sends spans or metrics: the ``endpoint`` (the exporter's default if ``None``)
    and the ``headers`` sent with them."""

    endpoint: str | None
    headers: Mapping[str, str]


def otlp_target(
    settings: TelemetrySettings,
    headers: Mapping[str, str],
    environ: Mapping[str, str],
    signal: Signal,
) -> OtlpTarget:
    """Where OTLP sends ``signal`` as ``settings`` say, with the headers file's ``headers``, the
    ``OTEL_EXPORTER_OTLP_*`` variables in ``environ`` over both, the signal's own over all."""
    sent = {
        **headers,
        **env_headers(environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")),
        **env_headers(environ.get(f"{_variable(signal)}_HEADERS", "")),
    }
    return OtlpTarget(_endpoint(settings, environ, signal), sent)


def _endpoint(
    settings: TelemetrySettings, environ: Mapping[str, str], signal: Signal
) -> str | None:
    """The endpoint ``signal`` goes to: its own in ``environ`` as it is, else the base URL in
    ``environ`` or ``settings``, with the signal's path added over HTTP."""
    own = environ.get(f"{_variable(signal)}_ENDPOINT")
    if own:
        return own
    base = environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or settings.endpoint
    if base is None or settings.protocol == "grpc":
        return base
    return f"{base.rstrip('/')}/v1/{signal}"


@dataclass(frozen=True)
class AgentCollector:
    """Where agents send the Claude Code CLI's own telemetry: to the collector at the base URL
    ``endpoint`` over ``protocol``, or nowhere when ``endpoint`` is ``None``."""

    endpoint: str | None = None
    protocol: OtlpProtocol = "http/protobuf"


def agent_collector(settings: TelemetrySettings, environ: Mapping[str, str]) -> AgentCollector:
    """Where agents send their telemetry as ``settings`` say: to the collector orbit's goes to,
    at the base URL ``OTEL_EXPORTER_OTLP_ENDPOINT`` in ``environ`` gives, else ``settings``',
    else OTLP's default; nowhere unless orbit's goes over OTLP."""
    if settings.exporter != "otlp":
        return AgentCollector()
    endpoint = (
        environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or settings.endpoint
        or DEFAULT_ENDPOINTS[settings.protocol]
    )
    return AgentCollector(endpoint, settings.protocol)


def _variable(signal: Signal) -> str:
    """The start of the names of ``signal``'s own variables, such as
    ``OTEL_EXPORTER_OTLP_TRACES``."""
    return f"OTEL_EXPORTER_OTLP_{signal.upper()}"


def env_headers(text: str) -> dict[str, str]:
    """The headers in ``text``, written as the ``OTEL_EXPORTER_OTLP_HEADERS`` variable writes
    them: ``name=value`` pairs separated by commas, percent-encoded; pairs without a name or
    ``=`` are ignored."""
    pairs = (pair.partition("=") for pair in text.split(","))
    return {
        unquote(name).strip(): unquote(value).strip()
        for name, equals, value in pairs
        if equals and name.strip()
    }
