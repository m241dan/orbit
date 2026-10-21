"""``orbit chat --remote``: the TUI over a control plane the user config names."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from orbit.agent.domain.server import ServerSettings
from orbit.cli import app
from orbit.client import WebSocketSession
from orbit.protocol import NewSession, Welcome
from tests.test_cli_connect import FakeApp, FakeClient
from tests.test_client_tls import HELLO, control_plane
from tests.tls_certs import make_ca, make_client, make_server

runner = CliRunner()
URL = "ws://127.0.0.1:7433/"


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch, config: Path) -> None:
    def no_backend(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("A connected chat builds no backend.")

    monkeypatch.setattr("orbit.cli.chat.OrbitApp", FakeApp)
    monkeypatch.setattr("orbit.cli.chat.WebSocketSession", FakeClient)
    monkeypatch.setattr("orbit.cli.chat._backend", no_backend)
    monkeypatch.setenv("ORBIT_CONFIG_DIR", str(config))
    FakeApp.built = []


@pytest.fixture
def config(tmp_path: Path) -> Path:
    """The config directory, holding the token."""
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "token").write_text("the-token\n")
    return directory


def _remotes(config: Path, text: str) -> Path:
    path = config / "config.toml"
    path.write_text(text)
    return path


def test_a_remote_is_connected_to_with_its_url_and_the_default_token(config: Path) -> None:
    _remotes(config, f'[remotes.home]\nurl = "{URL}"\n')
    result = runner.invoke(app, ["chat", "--remote", "home", "--new"])
    assert result.exit_code == 0, result.output
    (tui,) = FakeApp.built
    assert tui.session == FakeClient(URL, config / "token")
    assert tui.opening.command == NewSession() and tui.ran


def test_a_remotes_token_file_is_shown(config: Path) -> None:
    (config / "work-token").write_text("work\n")
    _remotes(config, f'[remotes.work]\nurl = "{URL}"\ntoken_file = "work-token"\n')
    assert runner.invoke(app, ["chat", "--remote", "work"]).exit_code == 0
    assert FakeApp.built[0].session == FakeClient(URL, config / "work-token")


def test_a_remotes_client_certificate_is_shown_rather_than_the_token(config: Path) -> None:
    ca = make_ca(config)
    client = make_client(ca, config)
    _remotes(
        config,
        '[remotes.work]\nurl = "wss://orbit.example.lan:7433/"\n'
        f'ca = "{ca.path}"\ncert = "{client.cert}"\nkey = "{client.key.name}"\n',
    )
    result = runner.invoke(app, ["chat", "--remote", "work"])
    assert result.exit_code == 0, result.output
    session = FakeApp.built[0].session
    assert isinstance(session, FakeClient) and session.token_file is None
    assert session.ssl_context is not None and session.ssl_context.check_hostname


def test_the_token_file_flag_overrides_the_remotes_credentials(config: Path) -> None:
    client = make_client(make_ca(config), config)
    other = config / "other"
    other.write_text("other\n")
    _remotes(
        config, f'[remotes.work]\nurl = "{URL}"\ncert = "{client.cert}"\nkey = "{client.key}"\n'
    )
    assert (
        runner.invoke(app, ["chat", "--remote", "work", "--token-file", str(other)]).exit_code == 0
    )
    assert FakeApp.built[0].session == FakeClient(URL, other)


def test_the_ca_flag_overrides_the_remotes(config: Path) -> None:
    ca = make_ca(config)
    _remotes(
        config,
        '[remotes.work]\nurl = "wss://orbit.example.lan/"\nca = "missing.pem"\n',
    )
    result = runner.invoke(app, ["chat", "--remote", "work", "--ca", str(ca.path)])
    assert result.exit_code == 0, result.output


def test_a_remotes_ca_that_cant_be_read_exits_naming_it(config: Path) -> None:
    _remotes(config, '[remotes.work]\nurl = "wss://orbit.example.lan/"\nca = "missing.pem"\n')
    result = runner.invoke(app, ["chat", "--remote", "work"])
    assert result.exit_code == 2
    assert f"The CA file can't be read. {config / 'missing.pem'}: " in result.stderr


def test_an_unknown_remote_exits_listing_those_configured(config: Path) -> None:
    path = _remotes(config, f'[remotes.work]\nurl = "{URL}"\n\n[remotes.home]\nurl = "{URL}"\n')
    result = runner.invoke(app, ["chat", "--remote", "nope"])
    assert result.exit_code == 2
    assert f"No remote named nope in {path}; the remotes configured are home, work." in (
        result.stderr
    )
    assert FakeApp.built == []


def test_an_unknown_remote_without_any_configured_says_so(config: Path) -> None:
    result = runner.invoke(app, ["chat", "--remote", "nope"])
    assert result.exit_code == 2
    assert (
        f"No remote named nope: none are configured in {config / 'config.toml'}; add a"
        " [remotes.nope] table" in result.stderr
    )


def test_an_invalid_remote_exits_naming_the_file_and_key(config: Path) -> None:
    path = _remotes(config, f'[remotes.work]\nurl = "{URL}"\ncert = "laptop.pem"\n')
    result = runner.invoke(app, ["chat", "--remote", "work"])
    assert result.exit_code == 2
    assert f"Invalid orbit configuration: {path}: remotes.work: cert needs key" in result.stderr


def test_remote_cant_go_with_connect(config: Path) -> None:
    _remotes(config, f'[remotes.work]\nurl = "{URL}"\n')
    result = runner.invoke(app, ["chat", "--remote", "work", "--connect", URL])
    assert result.exit_code == 2
    assert "--remote names the control plane to connect to, so it can't go with --connect" in (
        result.stderr
    )
    assert FakeApp.built == []


def test_remote_cant_go_with_local(config: Path) -> None:
    _remotes(config, f'[remotes.work]\nurl = "{URL}"\n')
    result = runner.invoke(app, ["chat", "--remote", "work", "--local"])
    assert result.exit_code == 2
    assert "--local runs the control plane here, so it can't go with --remote." in result.stderr
    assert FakeApp.built == []


def test_a_backend_flag_with_remote_exits_saying_it_belongs_on_serve(config: Path) -> None:
    _remotes(config, f'[remotes.work]\nurl = "{URL}"\n')
    result = runner.invoke(app, ["chat", "--remote", "work", "--model", "opus"])
    assert result.exit_code == 2
    assert (
        "--model configure the control plane, so they belong on orbit serve, not orbit chat"
        " --remote." in result.stderr
    )
    assert FakeApp.built == []


async def test_a_remote_over_tls_is_joined_with_its_ca_and_token_file(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("orbit.cli.chat.WebSocketSession", WebSocketSession)
    ca = make_ca(config)
    files = make_server(ca, config)
    settings = ServerSettings(port=0, tls="tls", cert=str(files.cert), key=str(files.key))
    async with control_plane(settings) as port:
        _remotes(
            config,
            f'[remotes.work]\nurl = "wss://localhost:{port}/"\nca = "{ca.path.name}"\n'
            'token_file = "token"\n',
        )
        result = runner.invoke(app, ["chat", "--remote", "work"])
        assert result.exit_code == 0, result.output
        session = FakeApp.built[0].session
        assert isinstance(session, WebSocketSession)
        events = session.events()
        try:
            await session.send(HELLO)
            first = await asyncio.wait_for(anext(aiter(events)), 5)
            assert type(first.payload) is Welcome
        finally:
            await session.close()
