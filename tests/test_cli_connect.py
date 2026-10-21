"""``orbit chat --connect``: the TUI over a control plane ``orbit serve`` runs."""

import ssl
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orbit.cli import app
from orbit.client import WebSocketSession, messages
from orbit.protocol import NewSession
from tests.cli_fakes import FakeApp, FakeClient
from tests.tls_certs import make_ca, make_client

runner = CliRunner()
URL = "ws://127.0.0.1:7433/"


pytestmark = pytest.mark.usefixtures("fakes")


@pytest.fixture
def token_file(config_dir: Path) -> Path:
    """The token file in the config directory."""
    path = config_dir / "token"
    path.parent.mkdir()
    path.write_text("the-token\n")
    return path


def test_connect_runs_the_tui_over_the_control_plane_with_the_config_dirs_token(
    token_file: Path,
) -> None:
    result = runner.invoke(app, ["chat", "--connect", URL])
    assert result.exit_code == 0, result.output
    (tui,) = FakeApp.built
    assert tui.session == FakeClient(URL, token_file)
    assert tui.link is tui.session and tui.opening.command is None and tui.ran
    assert tui.opening.pick  # The user picks a session once joined.


@pytest.mark.usefixtures("token_file")
def test_connect_opens_on_the_workspace_of_the_repository_it_runs_in(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    result = runner.invoke(app, ["chat", "--connect", URL, "-C", str(project)])
    assert result.exit_code == 0, result.output
    assert FakeApp.built[0].opening.repository == str(project.resolve())


def test_the_token_file_option_names_another_file(tmp_path: Path) -> None:
    other = tmp_path / "other-token"
    other.write_text("another\n")
    result = runner.invoke(app, ["chat", "--connect", URL, "--token-file", str(other)])
    assert result.exit_code == 0, result.output
    assert FakeApp.built[0].session == FakeClient(URL, other)


@pytest.mark.usefixtures("token_file")
def test_new_opens_a_new_session_once_welcomed() -> None:
    assert runner.invoke(app, ["chat", "--connect", URL, "--new"]).exit_code == 0
    assert FakeApp.built[0].opening.command == NewSession()
    assert not FakeApp.built[0].opening.pick


@pytest.mark.usefixtures("token_file")
def test_name_opens_a_new_session_with_that_name() -> None:
    assert runner.invoke(app, ["chat", "--connect", URL, "--name", "auth"]).exit_code == 0
    assert FakeApp.built[0].opening.command == NewSession(name="auth")


@pytest.mark.usefixtures("token_file")
@pytest.mark.parametrize(
    ("args", "named"),
    [
        (["--model", "opus"], "--model"),
        (["--tools", "full"], "--tools"),
        (["--no-verify"], "--verify"),
        (["--verify"], "--verify"),
        (["--no-worktree"], "--worktree"),
        (["--no-review"], "--review"),
        (["--no-usage"], "--usage"),
        (["--profile", "/tmp/p"], "--profile"),
    ],
)
def test_a_backend_flag_with_connect_exits_saying_it_belongs_on_serve(
    args: list[str], named: str
) -> None:
    result = runner.invoke(app, ["chat", "--connect", URL, *args])
    assert result.exit_code == 2
    assert f"{named} configure the control plane, so they belong on orbit serve" in result.stderr
    assert FakeApp.built == []


@pytest.mark.usefixtures("token_file")
def test_every_backend_flag_given_is_named() -> None:
    result = runner.invoke(app, ["chat", "--connect", URL, "--model", "opus", "--no-usage"])
    assert result.exit_code == 2
    assert result.stderr.startswith("--model, --usage configure the control plane")


@pytest.mark.usefixtures("token_file")
def test_a_url_other_than_ws_or_wss_exits() -> None:
    result = runner.invoke(app, ["chat", "--connect", "http://example.com/"])
    assert result.exit_code == 2
    assert "takes a wss:// or ws:// URL, not http://example.com/" in result.stderr
    assert FakeApp.built == []


@pytest.mark.usefixtures("token_file")
def test_a_wss_url_is_verified_by_the_ca_given(tmp_path: Path) -> None:
    ca = make_ca(tmp_path)
    url = "wss://orbit.example.lan:7433/"
    result = runner.invoke(app, ["chat", "--connect", url, "--ca", str(ca.path)])
    assert result.exit_code == 0, result.output
    client = FakeApp.built[0].session
    assert isinstance(client, FakeClient) and client.url == url
    assert client.ssl_context is not None and client.ssl_context.check_hostname
    subjects = [certificate["subject"] for certificate in client.ssl_context.get_ca_certs()]
    assert subjects == [((("commonName", "orbit test CA"),),)]


@pytest.mark.usefixtures("token_file")
def test_a_wss_url_without_a_ca_is_verified_by_the_systems_cas() -> None:
    result = runner.invoke(app, ["chat", "--connect", "wss://orbit.example.lan:7433/"])
    assert result.exit_code == 0, result.output
    client = FakeApp.built[0].session
    assert isinstance(client, FakeClient) and client.ssl_context is not None
    assert client.ssl_context.get_ca_certs() == ssl.create_default_context().get_ca_certs()


@pytest.mark.usefixtures("token_file")
def test_a_ws_url_has_no_tls_context(tmp_path: Path) -> None:
    result = runner.invoke(app, ["chat", "--connect", URL, "--ca", str(tmp_path / "unused")])
    assert result.exit_code == 0, result.output
    assert FakeApp.built[0].session == FakeClient(URL, tmp_path / "config" / "token")


@pytest.mark.usefixtures("token_file")
def test_a_ca_file_that_cant_be_read_exits_naming_it(tmp_path: Path) -> None:
    missing = tmp_path / "missing.pem"
    result = runner.invoke(
        app, ["chat", "--connect", "wss://orbit.example.lan/", "--ca", str(missing)]
    )
    assert result.exit_code == 2
    assert f"The CA file can't be read. {missing}: " in result.stderr
    assert FakeApp.built == []


@pytest.mark.usefixtures("token_file")
def test_plaintext_beyond_loopback_exits_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orbit.cli.chat.WebSocketSession", WebSocketSession)
    url = "ws://example.com:7433"
    result = runner.invoke(app, ["chat", "--connect", url])
    assert result.exit_code == 2
    assert messages.PLAINTEXT_NOT_LOOPBACK.format(url=url, host="example.com") in result.stderr
    assert FakeApp.built == []


def test_a_missing_token_file_exits_naming_it(tmp_path: Path) -> None:
    result = runner.invoke(app, ["chat", "--connect", URL])
    assert result.exit_code == 2
    assert f"The token file can't be read. {tmp_path / 'config' / 'token'}: " in result.stderr
    assert FakeApp.built == []


def test_a_client_certificate_is_shown_rather_than_the_token(tmp_path: Path) -> None:
    ca = make_ca(tmp_path)
    client = make_client(ca, tmp_path)
    url = "wss://orbit.example.lan:7433/"
    flags = ["--ca", str(ca.path), "--cert", str(client.cert), "--key", str(client.key)]
    result = runner.invoke(app, ["chat", "--connect", url, *flags])
    assert result.exit_code == 0, result.output
    session = FakeApp.built[0].session
    assert isinstance(session, FakeClient) and session.token_file is None
    assert session.ssl_context is not None


def test_a_client_certificate_cant_go_with_a_token_file(tmp_path: Path) -> None:
    client = make_client(make_ca(tmp_path), tmp_path)
    flags = ["--cert", str(client.cert), "--key", str(client.key)]
    result = runner.invoke(
        app, ["chat", "--connect", URL, *flags, "--token-file", str(tmp_path / "token")]
    )
    assert result.exit_code == 2
    assert "can't go with --token-file" in result.stderr
    assert FakeApp.built == []


def test_a_client_key_others_can_read_exits_with_the_chmod_that_fixes_it(tmp_path: Path) -> None:
    client = make_client(make_ca(tmp_path), tmp_path)
    client.key.chmod(0o644)
    flags = ["--cert", str(client.cert), "--key", str(client.key)]
    result = runner.invoke(app, ["chat", "--connect", "wss://orbit.example.lan/", *flags])
    assert result.exit_code == 2
    assert messages.KEY_READABLE.format(path=client.key) in result.stderr
    assert FakeApp.built == []
