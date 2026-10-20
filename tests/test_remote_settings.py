"""``[remotes.<name>]`` in the user's config file: the control planes ``orbit chat`` connects to
by name."""

import re
from pathlib import Path

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.remotes import RemoteSettings
from orbit.agent.external.user_config import read_remotes, read_server

URL = "wss://orbit.example.lan:7433/"


def _config(directory: Path, text: str) -> Path:
    path = directory / "config.toml"
    path.write_text(text)
    return path


def test_without_a_file_there_are_no_remotes(tmp_path: Path) -> None:
    assert read_remotes(tmp_path / "missing") == {}


def test_without_remotes_there_are_none_and_the_rest_is_ignored(tmp_path: Path) -> None:
    _config(tmp_path, "[storage]\ntool_output_kb = 1\n\n[server]\nport = 1\n")
    assert read_remotes(tmp_path) == {}


def test_each_remote_is_read_by_name(tmp_path: Path) -> None:
    _config(
        tmp_path,
        f'[remotes.work]\nurl = "{URL}"\n\n[remotes.home]\nurl = "ws://127.0.0.1:7433/"\n',
    )
    assert read_remotes(tmp_path) == {
        "work": RemoteSettings(url=URL),
        "home": RemoteSettings(url="ws://127.0.0.1:7433/"),
    }


def test_relative_paths_are_read_from_the_config_directory(tmp_path: Path) -> None:
    _config(tmp_path, f'[remotes.work]\nurl = "{URL}"\nca = "tls/ca.pem"\ntoken_file = "work"\n')
    assert read_remotes(tmp_path)["work"] == RemoteSettings(
        url=URL, ca=str(tmp_path / "tls" / "ca.pem"), token_file=str(tmp_path / "work")
    )


def test_absolute_paths_are_kept_and_home_is_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _config(
        tmp_path,
        f'[remotes.work]\nurl = "{URL}"\ncert = "/etc/laptop.pem"\nkey = "~/laptop-key.pem"\n',
    )
    assert read_remotes(tmp_path)["work"] == RemoteSettings(
        url=URL, cert="/etc/laptop.pem", key=str(tmp_path / "home" / "laptop-key.pem")
    )


def test_an_unknown_key_names_the_file_and_the_key(tmp_path: Path) -> None:
    path = _config(tmp_path, f'[remotes.work]\nurl = "{URL}"\ncolour = "blue"\n')
    with pytest.raises(ConfigError, match=f"^{re.escape(str(path))}: remotes.work.colour: "):
        read_remotes(tmp_path)


def test_a_remote_without_a_url_names_it(tmp_path: Path) -> None:
    _config(tmp_path, '[remotes.work]\nca = "ca.pem"\n')
    with pytest.raises(ConfigError, match=re.escape("remotes.work.url: Field required")):
        read_remotes(tmp_path)


def test_a_url_other_than_ws_or_wss_names_it(tmp_path: Path) -> None:
    _config(tmp_path, '[remotes.work]\nurl = "https://orbit.example.lan/"\n')
    with pytest.raises(ConfigError, match=re.escape("remotes.work.url: ")):
        read_remotes(tmp_path)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('cert = "laptop.pem"', "remotes.work: cert needs key, the client certificate's key"),
        ('key = "laptop-key.pem"', "remotes.work: key needs cert, the client certificate"),
    ],
)
def test_a_client_certificate_without_its_key_names_what_is_missing(
    tmp_path: Path, line: str, message: str
) -> None:
    _config(tmp_path, f'[remotes.work]\nurl = "{URL}"\n{line}\n')
    with pytest.raises(ConfigError, match=re.escape(message)):
        read_remotes(tmp_path)


def test_a_client_certificate_cant_go_with_a_token_file(tmp_path: Path) -> None:
    _config(
        tmp_path,
        f'[remotes.work]\nurl = "{URL}"\ncert = "c.pem"\nkey = "k.pem"\ntoken_file = "token"\n',
    )
    with pytest.raises(
        ConfigError, match=re.escape("remotes.work: cert and key show a client certificate")
    ):
        read_remotes(tmp_path)


def test_remotes_leave_the_server_settings_alone(tmp_path: Path) -> None:
    _config(tmp_path, f'[server]\nport = 1\n\n[remotes.work]\nurl = "{URL}"\n')
    assert read_server(tmp_path).port == 1
