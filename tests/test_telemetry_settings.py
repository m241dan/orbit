"""The user config's ``[telemetry]``, its headers file and the environment over them."""

from pathlib import Path

import pytest

from orbit.telemetry import (
    HeadersFileError,
    OtlpTarget,
    TelemetryConfigError,
    TelemetrySettings,
    env_headers,
    load_telemetry,
    otlp_target,
    parse_headers,
    read_headers,
)


def test_without_a_telemetry_table_telemetry_is_off() -> None:
    settings = load_telemetry({"storage": {"tool_output_kb": 1}})

    assert settings == TelemetrySettings()
    assert settings.exporter == "none"
    assert (settings.protocol, settings.file_max_mb, settings.file_keep) == ("http/protobuf", 50, 5)
    assert settings.service_name == "orbit"


def test_the_telemetry_table_sets_every_key() -> None:
    table = {
        "exporter": "otlp",
        "endpoint": "https://collector:4318",
        "protocol": "grpc",
        "headers_file": "otel-headers",
        "file_max_mb": 10,
        "file_keep": 3,
        "service_name": "orbit-ci",
    }

    assert load_telemetry({"telemetry": table}) == TelemetrySettings.model_validate(table)


@pytest.mark.parametrize(
    ("table", "key"),
    [
        ({"exporter": "jaeger"}, "telemetry.exporter"),
        ({"protocol": "http/json"}, "telemetry.protocol"),
        ({"file_max_mb": 0}, "telemetry.file_max_mb"),
        ({"file_keep": "5"}, "telemetry.file_keep"),
        ({"service_name": ""}, "telemetry.service_name"),
        ({"sampler": "always_on"}, "telemetry.sampler"),
    ],
)
def test_an_invalid_telemetry_setting_is_refused_naming_the_key(
    table: dict[str, object], key: str
) -> None:
    with pytest.raises(TelemetryConfigError, match=f"^{key}: "):
        load_telemetry({"telemetry": table})


@pytest.mark.parametrize(
    ("exporter", "protocol", "extra"),
    [
        ("otlp", "http/protobuf", "telemetry"),
        ("otlp", "grpc", "telemetry-grpc"),
        ("file", "grpc", "telemetry"),
        ("console", "http/protobuf", "telemetry"),
    ],
)
def test_the_extra_named_is_the_one_that_installs_the_exporter(
    exporter: str, protocol: str, extra: str
) -> None:
    settings = TelemetrySettings.model_validate({"exporter": exporter, "protocol": protocol})

    assert settings.extra == extra


def test_the_headers_file_is_taken_from_the_config_directory(tmp_path: Path) -> None:
    assert TelemetrySettings().headers_path(tmp_path) is None
    relative = TelemetrySettings(headers_file="otel/headers")
    assert relative.headers_path(tmp_path) == tmp_path / "otel" / "headers"
    absolute = TelemetrySettings(headers_file="/etc/otel-headers")
    assert absolute.headers_path(tmp_path) == Path("/etc/otel-headers")


def test_the_headers_file_has_its_tilde_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    path = TelemetrySettings(headers_file="~/otel-headers").headers_path(tmp_path / "config")

    assert path == tmp_path / "home" / "otel-headers"


def test_without_an_endpoint_the_exporter_chooses() -> None:
    assert otlp_target(TelemetrySettings(), {}, {}) == OtlpTarget(None, {})


def test_over_http_the_traces_path_is_added_to_the_endpoint() -> None:
    settings = TelemetrySettings(endpoint="http://collector:4318/")

    assert otlp_target(settings, {}, {}).endpoint == "http://collector:4318/v1/traces"


def test_over_grpc_the_endpoint_is_used_as_it_is() -> None:
    settings = TelemetrySettings(endpoint="collector:4317", protocol="grpc")

    assert otlp_target(settings, {}, {}).endpoint == "collector:4317"


def test_the_environments_endpoint_overrides_the_configured_one() -> None:
    settings = TelemetrySettings(endpoint="http://configured:4318")
    environ = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://env:4318"}

    assert otlp_target(settings, {}, environ).endpoint == "http://env:4318/v1/traces"


def test_the_environments_traces_endpoint_is_used_as_it_is() -> None:
    environ = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://env:4318",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://traces:4318/spans",
    }

    endpoint = otlp_target(TelemetrySettings(endpoint="http://c:1"), {}, environ).endpoint

    assert endpoint == "http://traces:4318/spans"


def test_the_environments_headers_override_the_files_by_name() -> None:
    environ = {
        "OTEL_EXPORTER_OTLP_HEADERS": "x-team=env,x-key=env",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "x-key=traces",
    }
    from_file = {"x-team": "file", "x-file": "file", "x-key": "file"}

    headers = otlp_target(TelemetrySettings(), from_file, environ).headers

    assert headers == {"x-team": "env", "x-file": "file", "x-key": "traces"}


def test_environment_headers_are_percent_decoded_ignoring_pairs_without_a_name() -> None:
    text = "Authorization=Bearer%20abc, x-a = 1 ,=nameless,novalue,x-b=a=b"

    assert env_headers(text) == {"Authorization": "Bearer abc", "x-a": "1", "x-b": "a=b"}
    assert env_headers("") == {}


def test_a_headers_file_has_one_header_a_line_ignoring_blanks_and_comments() -> None:
    text = "# The collector's key.\nAuthorization: Bearer a:b\n\n  x-team :  orbit \n"

    assert parse_headers(text) == {"Authorization": "Bearer a:b", "x-team": "orbit"}


@pytest.mark.parametrize("line", ["Authorization Bearer abc", ": value"])
def test_a_line_that_isnt_a_header_is_refused_naming_it(line: str) -> None:
    with pytest.raises(ValueError, match="line 2 isn't a header"):
        parse_headers(f"x-team: orbit\n{line}\n")


def _headers_file(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    """A headers file holding ``text`` with ``mode``."""
    path = tmp_path / "otel-headers"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_an_owner_only_headers_file_is_read(tmp_path: Path) -> None:
    path = _headers_file(tmp_path, "Authorization: Bearer abc\n")

    assert read_headers(path) == {"Authorization": "Bearer abc"}


def test_a_headers_file_others_can_read_is_refused_with_the_chmod_hint(tmp_path: Path) -> None:
    path = _headers_file(tmp_path, "Authorization: Bearer abc\n", 0o644)

    with pytest.raises(HeadersFileError) as raised:
        read_headers(path)
    assert str(raised.value) == (
        f"The telemetry headers file {path} can be read by others; run chmod 600 {path}."
    )


def test_a_missing_headers_file_is_refused_naming_it(tmp_path: Path) -> None:
    path = tmp_path / "missing"

    with pytest.raises(HeadersFileError, match=f"Can't read the telemetry headers file {path}: "):
        read_headers(path)


def test_a_headers_file_with_a_bad_line_is_refused_naming_the_line(tmp_path: Path) -> None:
    path = _headers_file(tmp_path, "nonsense\n")

    with pytest.raises(HeadersFileError, match="line 1 isn't a header"):
        read_headers(path)


def test_a_headers_file_that_isnt_utf8_is_refused(tmp_path: Path) -> None:
    path = _headers_file(tmp_path, "")
    path.write_bytes(b"x-a: \xff\n")

    with pytest.raises(HeadersFileError, match="Can't read the telemetry headers file"):
        read_headers(path)
