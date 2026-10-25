"""``orbit certs client --bundle``: a client's certificate, its key and the CA in an owner-only
tar, with the ``[remotes.<name>]`` lines that connect with them once unpacked on the client."""

import asyncio
import stat
import tarfile
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from orbit.agent.domain.server import ServerSettings
from orbit.agent.external.certificates import CertsDir
from orbit.agent.external.directories import CONFIG_DIR_VARIABLE
from orbit.cli import app
from orbit.client import WebSocketSession
from orbit.protocol import Welcome
from tests.cli_fakes import HELLO, FakeApp, control_plane

runner = CliRunner()


def _certs(config_dir: Path, *args: str) -> Result:
    return runner.invoke(app, ["certs", *args, "--config-dir", str(config_dir)])


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _members(bundle: Path) -> dict[str, tuple[int, bytes]]:
    """Each file in the tar ``bundle`` by name, in order: its mode and what's in it."""
    members: dict[str, tuple[int, bytes]] = {}
    with tarfile.open(bundle) as archive:
        for member in archive.getmembers():
            file = archive.extractfile(member)
            assert file is not None
            members[member.name] = (member.mode, file.read())
    return members


def _remote(bundle: Path) -> dict[str, str]:
    """The settings of the remote the ``remote.toml`` in ``bundle`` names."""
    [(_, remote)] = tomllib.loads(_members(bundle)["remote.toml"][1].decode())["remotes"].items()
    return dict(remote)


@pytest.fixture
def certs(config_dir: Path) -> CertsDir:
    """The files of the CA and server certificate ``orbit certs init`` made in ``config_dir``."""
    result = _certs(config_dir, "init", "box")
    assert result.exit_code == 0, result.output
    return CertsDir(config_dir)


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """Where the bundle goes."""
    return tmp_path / "laptop.tar"


def test_the_bundle_holds_the_ca_and_the_pair_written_and_is_owner_only(
    certs: CertsDir, config_dir: Path, bundle: Path
) -> None:
    result = _certs(config_dir, "client", "laptop", "--bundle", str(bundle))

    assert result.exit_code == 0, result.output
    assert _mode(bundle) == 0o600
    members = _members(bundle)
    assert list(members) == ["ca.pem", "laptop.pem", "laptop-key.pem", "remote.toml"]
    assert members["ca.pem"] == (0o644, certs.ca.read_bytes())
    assert members["laptop.pem"] == (0o644, certs.client("laptop").read_bytes())
    assert members["laptop-key.pem"] == (0o600, certs.client_key("laptop").read_bytes())
    assert members["remote.toml"][0] == 0o644


def test_the_bundles_remote_connects_to_the_server_certificates_first_name_on_7433(
    certs: CertsDir, config_dir: Path, bundle: Path
) -> None:
    _certs(config_dir, "client", "laptop", "--bundle", str(bundle))

    assert _remote(bundle) == {
        "url": "wss://box:7433",
        "ca": "~/.config/orbit/certs/laptop/ca.pem",
        "cert": "~/.config/orbit/certs/laptop/laptop.pem",
        "key": "~/.config/orbit/certs/laptop/laptop-key.pem",
    }


def test_the_default_url_takes_the_first_name_of_the_server_certificate_there_is(
    certs: CertsDir, config_dir: Path, bundle: Path
) -> None:
    _certs(config_dir, "server", "orbit.example.com", "10.0.0.5")
    _certs(config_dir, "client", "laptop", "--bundle", str(bundle))

    assert _remote(bundle)["url"] == "wss://orbit.example.com:7433"


def test_the_default_url_takes_the_port_in_server(
    certs: CertsDir, config_dir: Path, bundle: Path
) -> None:
    (config_dir / "config.toml").write_text("[server]\nport = 8443\n")
    _certs(config_dir, "client", "laptop", "--bundle", str(bundle))

    assert _remote(bundle)["url"] == "wss://box:8443"


def test_url_sets_the_remotes_url(certs: CertsDir, config_dir: Path, bundle: Path) -> None:
    url = "wss://orbit.example.com:9000"
    result = _certs(config_dir, "client", "laptop", "--bundle", str(bundle), "--url", url)

    assert result.exit_code == 0, result.output
    assert _remote(bundle)["url"] == url


def test_url_needs_no_server_certificate(certs: CertsDir, config_dir: Path, bundle: Path) -> None:
    certs.server.unlink()
    url = "wss://orbit.example.com:9000"
    result = _certs(config_dir, "client", "laptop", "--bundle", str(bundle), "--url", url)

    assert result.exit_code == 0, result.output
    assert _remote(bundle)["url"] == url


def test_client_says_where_the_bundle_is_and_how_to_use_it_on_the_client(
    certs: CertsDir, config_dir: Path, bundle: Path
) -> None:
    result = _certs(config_dir, "client", "laptop", "--bundle", str(bundle))

    assert result.stdout.endswith(
        f"  {certs.client('laptop')}\n  {certs.client_key('laptop')}\n\n"
        f"Bundled them with {certs.ca} and a remote.toml naming them in {bundle}, readable by"
        " you alone. Copy it to the client machine over a trusted channel, such as scp, and"
        " there unpack it and add the remote to orbit's config:\n\n"
        "  mkdir -p -m 700 ~/.config/orbit/certs/laptop\n"
        "  tar -xpf laptop.tar -C ~/.config/orbit/certs/laptop\n"
        "  cat ~/.config/orbit/certs/laptop/remote.toml >> ~/.config/orbit/config.toml\n\n"
        "Then connect with orbit chat --remote laptop.\n"
    )


def test_an_existing_bundle_is_refused_issuing_nothing(
    certs: CertsDir, config_dir: Path, bundle: Path
) -> None:
    bundle.write_text("mine")
    result = _certs(config_dir, "client", "laptop", "--bundle", str(bundle))

    assert result.exit_code == 2
    assert result.stderr == (
        f"{bundle} exists; use --force to replace it, or give --bundle another file.\n"
    )
    assert bundle.read_text() == "mine"
    assert not (certs.path / "clients").exists()


def test_force_replaces_the_bundle_with_the_new_pair(
    certs: CertsDir, config_dir: Path, bundle: Path
) -> None:
    _certs(config_dir, "client", "laptop", "--bundle", str(bundle))
    first = _members(bundle)["laptop.pem"]
    result = _certs(config_dir, "client", "laptop", "--bundle", str(bundle), "--force")

    assert result.exit_code == 0, result.output
    members = _members(bundle)
    assert members["laptop.pem"] != first
    assert members["laptop.pem"] == (0o644, certs.client("laptop").read_bytes())
    assert members["laptop-key.pem"] == (0o600, certs.client_key("laptop").read_bytes())
    assert _mode(bundle) == 0o600
    assert "to [server] revoked to stop the old certificate working.\n" in result.stdout


def test_url_without_bundle_is_refused_issuing_nothing(certs: CertsDir, config_dir: Path) -> None:
    result = _certs(config_dir, "client", "laptop", "--url", "wss://orbit.example.com:9000")

    assert result.exit_code == 2
    assert result.stderr == (
        "--url sets the URL in the bundle's remote.toml, so it needs --bundle.\n"
    )
    assert not (certs.path / "clients").exists()


def test_the_default_url_without_a_server_certificate_is_refused_naming_url(
    certs: CertsDir, config_dir: Path, bundle: Path
) -> None:
    certs.server.unlink()
    result = _certs(config_dir, "client", "laptop", "--bundle", str(bundle))

    assert result.exit_code == 2
    assert result.stderr == (
        f"There's no server certificate at {certs.server} to name the server in the bundle's"
        " URL; pass --url with the URL clients connect to, or make one with orbit certs"
        " server.\n"
    )
    assert not (certs.path / "clients").exists()
    assert not bundle.exists()


def test_the_default_url_with_an_invalid_config_is_refused_issuing_nothing(
    certs: CertsDir, config_dir: Path, bundle: Path
) -> None:
    (config_dir / "config.toml").write_text('[server]\nport = "high"\n')
    result = _certs(config_dir, "client", "laptop", "--bundle", str(bundle))

    assert result.exit_code == 2
    assert result.stderr.startswith("Invalid orbit configuration: ")
    assert "server.port" in result.stderr
    assert not (certs.path / "clients").exists()


def test_a_bundle_that_cant_be_written_is_refused_naming_it(
    certs: CertsDir, config_dir: Path, tmp_path: Path
) -> None:
    bundle = tmp_path / "missing" / "laptop.tar"
    result = _certs(config_dir, "client", "laptop", "--bundle", str(bundle))

    assert result.exit_code == 2
    assert result.stderr == (
        f"Can't write {bundle}: No such file or directory. Check that you can write to"
        f" {bundle.parent}.\n"
    )


def _unpack(bundle: Path, directory: Path) -> Path:
    """``directory``, made owner-only, with ``bundle`` unpacked into it, keeping its modes."""
    directory.mkdir(mode=0o700, parents=True)
    with tarfile.open(bundle) as archive:
        archive.extractall(directory, filter="data")
    return directory


async def _welcomed(session: WebSocketSession) -> bool:
    """Whether the control plane ``session`` connects to welcomes it."""
    events = session.events()
    try:
        await session.send(HELLO)
        first = await asyncio.wait_for(anext(aiter(events)), 5)
        return type(first.payload) is Welcome
    finally:
        await session.close()


@pytest.mark.usefixtures("fakes")
async def test_an_unpacked_bundle_connects_its_remote_to_a_server_serving_mutual_tls(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "laptop"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(CONFIG_DIR_VARIABLE, str(home / ".config" / "orbit"))
    monkeypatch.setattr("orbit.cli.chat.WebSocketSession", WebSocketSession)
    assert _certs(config_dir, "init", "localhost").exit_code == 0
    certs = CertsDir(config_dir)
    settings = ServerSettings(
        port=0,
        tls="mtls",
        cert=str(certs.server),
        key=str(certs.server_key),
        client_ca=str(certs.ca),
    )
    async with control_plane(settings) as port:
        (config_dir / "config.toml").write_text(f"[server]\nport = {port}\n")
        bundle = tmp_path / "laptop.tar"
        assert _certs(config_dir, "client", "laptop", "--bundle", str(bundle)).exit_code == 0
        unpacked = _unpack(bundle, home / ".config" / "orbit" / "certs" / "laptop")
        assert _mode(unpacked / "laptop-key.pem") == 0o600
        (home / ".config" / "orbit" / "config.toml").write_text(
            (unpacked / "remote.toml").read_text()
        )
        result = runner.invoke(app, ["chat", "--remote", "laptop"])

        assert result.exit_code == 0, result.output
        session = FakeApp.built[0].session
        assert isinstance(session, WebSocketSession)
        assert await _welcomed(session)
