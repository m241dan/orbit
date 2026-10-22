"""The user config's ``[telemetry]``, and where spans go once the environment has its say."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Exporter = Literal["none", "otlp", "console", "file"]
"""Where spans go: nowhere, to a collector over OTLP, to stderr, or to files in the state
directory."""

OtlpProtocol = Literal["http/protobuf", "grpc"]
"""How spans reach a collector over OTLP."""

TRACES_PATH = "/v1/traces"
"""What OTLP over HTTP adds to a collector's base URL for spans."""


class TelemetryConfigError(ValueError):
    """``[telemetry]`` is invalid; the message says where and why, on one line per problem."""


class TelemetrySettings(BaseModel):
    """The user config's ``[telemetry]``: the ``exporter`` spans go to, and for OTLP the
    collector's base URL ``endpoint`` (the exporter's default if ``None``), the ``protocol``,
    and the ``headers_file`` holding the headers sent; the files the file exporter writes grow
    to ``file_max_mb`` each, ``file_keep`` of them kept; ``service_name`` names orbit to the
    collector."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    exporter: Exporter = "none"
    endpoint: str | None = Field(default=None, min_length=1)
    protocol: OtlpProtocol = "http/protobuf"
    headers_file: str | None = Field(default=None, min_length=1)
    file_max_mb: int = Field(default=50, ge=1)
    file_keep: int = Field(default=5, ge=1)
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
    """Where OTLP sends spans: the ``endpoint`` (the exporter's default if ``None``) and the
    ``headers`` sent with them."""

    endpoint: str | None
    headers: Mapping[str, str]


def otlp_target(
    settings: TelemetrySettings, headers: Mapping[str, str], environ: Mapping[str, str]
) -> OtlpTarget:
    """Where OTLP sends spans as ``settings`` say, with the headers file's ``headers``, the
    ``OTEL_EXPORTER_OTLP_*`` variables in ``environ`` over both."""
    sent = {
        **headers,
        **env_headers(environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")),
        **env_headers(environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "")),
    }
    return OtlpTarget(_endpoint(settings, environ), sent)


def _endpoint(settings: TelemetrySettings, environ: Mapping[str, str]) -> str | None:
    """The endpoint spans go to: the traces one in ``environ`` as it is, else the base URL in
    ``environ`` or ``settings``, with the traces path added over HTTP."""
    traces = environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if traces:
        return traces
    base = environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or settings.endpoint
    if base is None or settings.protocol == "grpc":
        return base
    return base.rstrip("/") + TRACES_PATH


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
