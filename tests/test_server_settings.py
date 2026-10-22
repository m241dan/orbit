"""``[server]`` in the user's config file: where ``orbit serve`` listens."""

import re
from pathlib import Path

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.server import (
    LocalTls,
    RunningServer,
    ServerSettings,
    is_loopback,
    local_candidates,
    local_hostname,
)
from orbit.agent.external.user_config import read_server


def test_without_a_file_the_server_defaults_apply(tmp_path: Path) -> None:
    settings = read_server(tmp_path / "missing")
    assert settings == ServerSettings(host="127.0.0.1", port=7433, max_message_mb=64)
    assert settings.max_coders == 4


def test_the_files_coder_cap_is_read_and_0_means_none(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[server]\nmax_coders = 0\n")
    assert read_server(tmp_path).max_coders == 0


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
        tls="tls",
        cert=str(tmp_path / "s.pem"),
        key=str(tmp_path / "k.pem"),
        client_ca=str(tmp_path / "c.pem"),
        ca=str(tmp_path / "a.pem"),
    )


def test_the_files_relative_tls_paths_are_read_from_the_config_directory(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[server]\ncert = "tls/server.pem"\n')
    assert read_server(tmp_path).cert == str(tmp_path / "tls" / "server.pem")


def test_the_files_absolute_tls_paths_are_kept_and_home_is_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "config.toml").write_text(
        '[server]\ncert = "/etc/server.pem"\nkey = "~/.config/orbit/tls/server-key.pem"\n'
    )
    settings = read_server(tmp_path)
    home_key = tmp_path / "home" / ".config" / "orbit" / "tls" / "server-key.pem"
    assert (settings.cert, settings.key) == ("/etc/server.pem", str(home_key))


def test_the_files_local_client_certificate_is_read(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[server]\nlocal_client_cert = "me.pem"\nlocal_client_key = "me-key.pem"\n'
    )
    settings = read_server(tmp_path)
    assert (settings.local_client_cert, settings.local_client_key) == (
        str(tmp_path / "me.pem"),
        str(tmp_path / "me-key.pem"),
    )


SHA256 = ":".join(["3F"] * 31 + ["9C"])
"""A SHA-256 fingerprint, as ``orbit serve``'s connect line shows it."""


def test_the_files_revoked_fingerprints_are_read(tmp_path: Path) -> None:
    other = "AB" * 32
    (tmp_path / "config.toml").write_text(f'[server]\nrevoked = ["{SHA256}", "{other}"]\n')
    assert read_server(tmp_path).revoked == (SHA256, other)


@pytest.mark.parametrize(
    "entry", [SHA256, SHA256.lower(), SHA256.replace(":", ""), SHA256.replace(":", " ")]
)
def test_a_full_sha256_fingerprint_is_revoked_in_any_accepted_form(entry: str) -> None:
    assert ServerSettings(revoked=(entry,)).revoked == (entry,)


@pytest.mark.parametrize(
    "entry",
    ["3F:A2:9C", SHA256[:-1] + "G", ":".join(["3F"] * 20)],
    ids=["truncated", "not hex", "sha-1"],
)
def test_a_revoked_entry_that_isnt_a_sha256_fingerprint_is_refused_by_name(
    tmp_path: Path, entry: str
) -> None:
    (tmp_path / "config.toml").write_text(f'[server]\nrevoked = ["{SHA256}", "{entry}"]\n')
    with pytest.raises(ConfigError) as refused:
        read_server(tmp_path)
    message = str(refused.value)
    assert "server.revoked" in message and repr(entry) in message
    assert "SHA-256 fingerprint" in message and "-fingerprint -sha256" in message


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
        ("[server]\nmax_coders = -1\n", "server.max_coders"),
        ("[server]\nmax_coders = '2'\n", "server.max_coders"),
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


@pytest.mark.parametrize(
    ("host", "candidates"),
    [
        ("0.0.0.0", ("localhost", "127.0.0.1")),
        ("::", ("localhost", "::1", "127.0.0.1")),
    ],
)
def test_a_wildcard_host_is_reached_by_the_loopback_addresses_it_listens_on(
    host: str, candidates: tuple[str, ...]
) -> None:
    assert local_candidates(host) == candidates


@pytest.mark.parametrize(
    ("host", "names", "hostname"),
    [
        ("127.0.0.1", ("localhost", "127.0.0.1"), "127.0.0.1"),
        ("127.0.0.1", ("LOCALHOST",), "localhost"),
        ("::1", ("0:0:0:0:0:0:0:1",), "::1"),
        ("0.0.0.0", ("orbit.example.lan", "localhost"), "localhost"),
        ("0.0.0.0", ("127.0.0.1",), "127.0.0.1"),
        ("::", ("::1",), "::1"),
        ("::", ("127.0.0.1", "::1"), "::1"),
        ("::", ("127.0.0.1",), "127.0.0.1"),
        ("orbit.example.lan", ("Orbit.Example.Lan",), "orbit.example.lan"),
        ("orbit.example.lan", ("*.example.lan",), "orbit.example.lan"),
        ("192.168.1.20", ("localhost", "192.168.1.20"), "192.168.1.20"),
    ],
)
def test_a_local_client_connects_by_a_name_the_certificate_gives(
    host: str, names: tuple[str, ...], hostname: str
) -> None:
    assert local_hostname(host, names) == hostname


@pytest.mark.parametrize(
    ("host", "names"),
    [
        ("0.0.0.0", ("orbit.example.lan",)),
        ("0.0.0.0", ("::1",)),
        ("192.168.1.20", ("localhost", "127.0.0.1")),
        ("orbit.example.lan", ("*.lan", "*.other.lan")),
        ("example.lan", ("*.example.lan",)),
        ("127.0.0.1", ()),
    ],
)
def test_a_certificate_naming_no_local_name_gives_no_hostname(
    host: str, names: tuple[str, ...]
) -> None:
    assert local_hostname(host, names) is None


def test_a_plaintext_server_is_reached_at_its_url() -> None:
    assert RunningServer(url="ws://127.0.0.1:7433/", pid=1).local_url == "ws://127.0.0.1:7433/"


@pytest.mark.parametrize(
    ("hostname", "url"),
    [("localhost", "wss://localhost:7433/"), ("::1", "wss://[::1]:7433/")],
)
def test_a_tls_server_is_reached_by_the_hostname_its_certificate_gives(
    hostname: str, url: str
) -> None:
    local = LocalTls(mutual=False, ca="/ca.pem", hostname=hostname)
    assert RunningServer(url="wss://0.0.0.0:7433/", pid=1, tls=local).local_url == url


def test_without_a_browse_root_everything_readable_is_browsable(tmp_path: Path) -> None:
    assert read_server(tmp_path).browse_root is None


def test_the_files_browse_root_is_read_with_its_symlinks_followed(tmp_path: Path) -> None:
    (tmp_path / "srv").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "srv")
    (tmp_path / "config.toml").write_text(f'[server]\nbrowse_root = "{tmp_path / "link"}"\n')
    assert read_server(tmp_path).browse_root == str((tmp_path / "srv").resolve())


@pytest.mark.parametrize("root", ["srv", "notes.txt", "/nowhere/at/all"])
def test_a_browse_root_that_isnt_an_absolute_directory_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: str
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "srv").mkdir()
    (tmp_path / "notes.txt").write_text("")
    given = str(tmp_path / root) if root == "notes.txt" else root
    (tmp_path / "config.toml").write_text(f'[server]\nbrowse_root = "{given}"\n')
    problem = f"server.browse_root: {given} isn't an absolute path to a directory"
    with pytest.raises(ConfigError) as raised:
        read_server(tmp_path)
    assert str(raised.value) == f"{tmp_path / 'config.toml'}: {problem}"
