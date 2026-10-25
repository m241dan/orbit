"""``orbit certs init``: a private CA and a server certificate it signs, written to ``certs`` in
the config directory, with the ``[server]`` lines that serve mutual TLS with them."""

import ipaddress
import socket
import stat
import tomllib
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from typer.testing import CliRunner, Result

from orbit.agent.external.certificates import CertsDir
from orbit.cli import app

runner = CliRunner()


def _init(config_dir: Path, *args: str) -> Result:
    return runner.invoke(app, ["certs", "init", *args, "--config-dir", str(config_dir)])


def _certificate(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _alternative_names(path: Path) -> list[x509.GeneralName]:
    extension = _certificate(path).extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return list(extension.value)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _files(certs: CertsDir) -> list[Path]:
    return [certs.ca, certs.ca_key, certs.server, certs.server_key]


def test_init_writes_the_ca_and_server_files_keys_owner_only(config_dir: Path) -> None:
    result = _init(config_dir, "orbit.example.com", "10.0.0.5")

    assert result.exit_code == 0, result.output
    certs = CertsDir(config_dir)
    assert sorted(certs.path.iterdir()) == sorted(_files(certs))
    assert _mode(certs.path) == 0o700
    assert _mode(certs.ca_key) == _mode(certs.server_key) == 0o600


def test_init_makes_a_server_certificate_for_the_names_and_this_machine(
    config_dir: Path,
) -> None:
    result = _init(config_dir, "orbit.example.com", "10.0.0.5")

    assert result.exit_code == 0, result.output
    certs = CertsDir(config_dir)
    assert _alternative_names(certs.server) == [
        x509.DNSName("orbit.example.com"),
        x509.IPAddress(ipaddress.ip_address("10.0.0.5")),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    _certificate(certs.server).verify_directly_issued_by(_certificate(certs.ca))


def test_init_without_names_makes_the_certificate_for_the_machines_hostname(
    config_dir: Path,
) -> None:
    result = _init(config_dir)

    assert result.exit_code == 0, result.output
    certs = CertsDir(config_dir)
    hostname = socket.gethostname()
    assert x509.DNSName(hostname) in _alternative_names(certs.server)
    [common_name] = _certificate(certs.ca).subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    assert common_name.value == f"orbit CA ({hostname})"


def test_init_for_a_name_longer_than_a_common_name_holds_it_whole(config_dir: Path) -> None:
    name = f"{'x' * 88}.example.com"
    result = _init(config_dir, name)

    assert result.exit_code == 0, result.output
    certs = CertsDir(config_dir)
    assert sorted(certs.path.iterdir()) == sorted(_files(certs))
    assert _alternative_names(certs.server)[0] == x509.DNSName(name)
    _certificate(certs.server).verify_directly_issued_by(_certificate(certs.ca))


def test_init_on_a_machine_with_a_long_hostname_names_its_ca_for_as_much_as_fits(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostname = "box-" * 15
    monkeypatch.setattr(socket, "gethostname", lambda: hostname)
    result = _init(config_dir, "orbit.example.com")

    assert result.exit_code == 0, result.output
    certs = CertsDir(config_dir)
    [common_name] = _certificate(certs.ca).subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    assert common_name.value == f"orbit CA ({hostname[:50]}…)"


def test_init_makes_ed25519_keys_by_default(config_dir: Path) -> None:
    _init(config_dir, "box")

    certs = CertsDir(config_dir)
    for key in (certs.ca_key, certs.server_key):
        assert isinstance(load_pem_private_key(key.read_bytes(), None), ed25519.Ed25519PrivateKey)


def test_init_makes_p256_keys_when_asked(config_dir: Path) -> None:
    result = _init(config_dir, "box", "--key-type", "p256")

    assert result.exit_code == 0, result.output
    certs = CertsDir(config_dir)
    for path in (certs.ca_key, certs.server_key):
        key = load_pem_private_key(path.read_bytes(), None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        assert isinstance(key.curve, ec.SECP256R1)


def _lifetime(path: Path) -> timedelta:
    certificate = _certificate(path)
    return certificate.not_valid_after_utc - certificate.not_valid_before_utc


def test_init_makes_a_ca_for_ten_years_and_a_server_certificate_for_one(config_dir: Path) -> None:
    _init(config_dir, "box")

    certs = CertsDir(config_dir)
    ca = _certificate(certs.ca)
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    usage = _certificate(certs.server).extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert list(usage.value) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert _lifetime(certs.ca) == timedelta(days=3650)
    assert _lifetime(certs.server) == timedelta(days=365)


def test_days_set_the_server_certificates_lifetime_alone(config_dir: Path) -> None:
    result = _init(config_dir, "box", "--days", "30")

    assert result.exit_code == 0, result.output
    certs = CertsDir(config_dir)
    assert _lifetime(certs.ca) == timedelta(days=3650)
    assert _lifetime(certs.server) == timedelta(days=30)


def _server_lines(output: str) -> str:
    """The ``[server]`` table in ``output``, up to the blank line after it."""
    table = output[output.index("[server]\n") :]
    return table[: table.index("\n\n") + 1]


def test_init_names_the_files_and_prints_the_server_lines_for_mutual_tls(
    config_dir: Path,
) -> None:
    result = _init(config_dir, "orbit.example.com", "10.0.0.5")

    certs = CertsDir(config_dir)
    assert result.stdout == (
        "Wrote a CA and a server certificate for orbit.example.com, 10.0.0.5, localhost and"
        f" 127.0.0.1:\n  {certs.ca}\n  {certs.ca_key}\n  {certs.server}\n  {certs.server_key}\n"
        f"\nTo serve mutual TLS with them, put these lines in {config_dir / 'config.toml'}, in"
        " its [server] table if it has one:\n\n"
        f'[server]\ntls = "mtls"\ncert = "{certs.server}"\nkey = "{certs.server_key}"\n'
        f'client_ca = "{certs.ca}"\n\n'
        f'For TLS without client certificates, use tls = "tls", with ca = "{certs.ca}" in'
        " place of client_ca.\n"
    )


def test_the_server_lines_are_toml_naming_any_path(tmp_path: Path) -> None:
    config_dir = tmp_path / 'my "orbit" \\ config'
    result = _init(config_dir, "box")

    certs = CertsDir(config_dir)
    assert tomllib.loads(_server_lines(result.stdout)) == {
        "server": {
            "tls": "mtls",
            "cert": str(certs.server),
            "key": str(certs.server_key),
            "client_ca": str(certs.ca),
        }
    }


def test_init_uses_the_config_directory_in_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORBIT_CONFIG_DIR", str(tmp_path / "elsewhere"))
    result = runner.invoke(app, ["certs", "init", "box"])

    assert result.exit_code == 0, result.output
    assert CertsDir(tmp_path / "elsewhere").ca.exists()


def test_a_second_init_is_refused_leaving_the_ca_alone(config_dir: Path) -> None:
    _init(config_dir, "box")
    certs = CertsDir(config_dir)
    before = [path.read_bytes() for path in _files(certs)]
    result = _init(config_dir, "box")

    assert result.exit_code == 2
    assert result.stderr == (
        f"A CA already exists in {certs.path}; a new one would invalidate every client"
        " certificate it signed. Use orbit certs server or orbit certs client, or --force to"
        " replace it.\n"
    )
    assert [path.read_bytes() for path in _files(certs)] == before


def test_init_with_force_sets_the_old_files_aside_and_writes_new_ones(config_dir: Path) -> None:
    _init(config_dir, "box")
    certs = CertsDir(config_dir)
    before = {path.name: path.read_bytes() for path in _files(certs)}
    result = _init(config_dir, "box", "--force")

    assert result.exit_code == 0, result.output
    [old] = certs.path.glob("old-*")
    assert {path.name: path.read_bytes() for path in old.iterdir()} == before
    assert all(path.read_bytes() != before[path.name] for path in _files(certs))
    assert result.stdout.startswith(f"Moved the old files to {old}.\nWrote a CA")


def test_init_sets_aside_the_files_a_ca_left_without_its_certificate(config_dir: Path) -> None:
    _init(config_dir, "box")
    certs = CertsDir(config_dir)
    certs.ca.unlink()
    server = certs.server.read_bytes()
    result = _init(config_dir, "box")

    assert result.exit_code == 0, result.output
    [old] = certs.path.glob("old-*")
    assert sorted(path.name for path in old.iterdir()) == [
        "ca-key.pem",
        "server-key.pem",
        "server.pem",
    ]
    assert (old / "server.pem").read_bytes() == server


@pytest.mark.parametrize("name", ["two words", "-", ".", "a..b", "a*b", "box-.example.com"])
def test_a_name_that_isnt_a_hostname_is_refused_setting_nothing_aside(
    config_dir: Path, name: str
) -> None:
    _init(config_dir, "box")
    certs = CertsDir(config_dir)
    result = _init(config_dir, name, "--force")

    assert result.exit_code == 2
    assert result.stderr.startswith(
        f"Can't make a certificate for \"{name}\": it's neither a hostname nor an IP address."
    )
    assert not list(certs.path.glob("old-*"))
    assert all(path.exists() for path in _files(certs))


def test_a_config_directory_that_cant_be_written_is_refused_with_the_fix(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.write_text("not a directory")
    result = _init(config, "box")

    assert result.exit_code == 2
    assert result.stderr.startswith(f"Can't write {CertsDir(config).ca_key}: Not a directory.")
    assert "--config-dir" in result.stderr
    assert "Traceback" not in result.output


@pytest.mark.parametrize("args", [("--days", "0"), ("--days", "3651"), ("--key-type", "rsa")])
def test_a_lifetime_or_key_type_out_of_range_is_refused(
    config_dir: Path, args: tuple[str, ...]
) -> None:
    result = _init(config_dir, "box", *args)

    assert result.exit_code == 2
    assert not CertsDir(config_dir).path.exists()
