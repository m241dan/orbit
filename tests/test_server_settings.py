"""``[server]`` in the user's config file: where ``orbit serve`` listens."""

import re
from pathlib import Path

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.server import ServerSettings, is_loopback
from orbit.agent.external.user_config import read_server


def test_without_a_file_the_server_defaults_apply(tmp_path: Path) -> None:
    settings = read_server(tmp_path / "missing")
    assert settings == ServerSettings(host="127.0.0.1", port=7433, max_message_mb=64)


def test_the_files_server_settings_are_read_and_the_rest_ignored(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[storage]\ntool_output_kb = 1\n\n[server]\nhost = "::1"\nport = 0\nmax_message_mb = 2\n'
    )
    assert read_server(tmp_path) == ServerSettings(host="::1", port=0, max_message_mb=2)


def test_the_files_tls_settings_are_read(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[server]\ntls = "tls"\ncert = "s.pem"\nkey = "k.pem"\nclient_ca = "c.pem"\nca = "a.pem"\n'
    )
    assert read_server(tmp_path) == ServerSettings(
        tls="tls", cert="s.pem", key="k.pem", client_ca="c.pem", ca="a.pem"
    )


def test_the_files_revoked_fingerprints_are_read(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[server]\nrevoked = ["AB:CD", "EF:01"]\n')
    assert read_server(tmp_path).revoked == ("AB:CD", "EF:01")


def test_revoked_must_hold_fingerprints(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[server]\nrevoked = [1]\n")
    with pytest.raises(ConfigError, match="revoked"):
        read_server(tmp_path)


def test_the_message_limit_is_counted_in_mebibytes() -> None:
    assert ServerSettings(max_message_mb=2).max_message_bytes == 2 * 1024 * 1024


@pytest.mark.parametrize(
    ("table", "key"),
    [
        ("[server]\nhots = 'x'\n", "server.hots"),
        ("[server]\nport = 70000\n", "server.port"),
        ("[server]\nport = '7433'\n", "server.port"),
        ("[server]\nmax_message_mb = 0\n", "server.max_message_mb"),
        ("[server]\ntls = 'on'\n", "server.tls"),
        ("[server]\ncert = 1\n", "server.cert"),
    ],
)
def test_an_invalid_setting_names_the_file_and_the_key(
    tmp_path: Path, table: str, key: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(table)
    with pytest.raises(ConfigError, match=f"^{re.escape(str(path))}: {re.escape(key)}: "):
        read_server(tmp_path)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.1.2.3", "::1", "localhost", "LocalHost"])
def test_loopback_hosts_are_loopback(host: str) -> None:
    assert is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.2", "::", "example.com"])
def test_other_hosts_are_not_loopback(host: str) -> None:
    assert not is_loopback(host)
