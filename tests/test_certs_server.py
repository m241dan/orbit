"""``orbit certs server``: a new server certificate the CA signs, with the old one set aside in
``certs`` in the config directory."""

import ipaddress
import socket
import stat
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from typer.testing import CliRunner, Result

from orbit.agent.external.certificates import CertsDir
from orbit.cli import app

runner = CliRunner()


def _certs(config_dir: Path, *args: str) -> Result:
    return runner.invoke(app, ["certs", *args, "--config-dir", str(config_dir)])


def _certificate(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _alternative_names(path: Path) -> list[x509.GeneralName]:
    extension = _certificate(path).extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return list(extension.value)


def _contents(*paths: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in paths}


@pytest.fixture
def certs(config_dir: Path) -> CertsDir:
    """The files of the CA and server certificate ``orbit certs init`` made in ``config_dir``."""
    result = _certs(config_dir, "init", "box")
    assert result.exit_code == 0, result.output
    return CertsDir(config_dir)


def test_server_sets_the_old_pair_aside_and_writes_one_for_the_names_given(
    certs: CertsDir, config_dir: Path
) -> None:
    ca = _contents(certs.ca, certs.ca_key)
    before = _contents(certs.server, certs.server_key)
    result = _certs(config_dir, "server", "orbit.example.com")

    assert result.exit_code == 0, result.output
    [old] = certs.path.glob("old-*")
    assert _contents(*old.iterdir()) == before
    assert _alternative_names(certs.server) == [
        x509.DNSName("orbit.example.com"),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    _certificate(certs.server).verify_directly_issued_by(_certificate(certs.ca))
    assert _contents(certs.ca, certs.ca_key) == ca
    assert stat.S_IMODE(certs.server_key.stat().st_mode) == 0o600


def test_server_makes_a_certificate_for_server_authentication_of_its_first_name(
    certs: CertsDir, config_dir: Path
) -> None:
    _certs(config_dir, "server", "orbit.example.com", "10.0.0.5")

    certificate = _certificate(certs.server)
    [common_name] = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    assert common_name.value == "orbit.example.com"
    usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert list(usage.value) == [ExtendedKeyUsageOID.SERVER_AUTH]
    key = load_pem_private_key(certs.server_key.read_bytes(), None)
    assert key.public_key() == certificate.public_key()


def test_server_names_the_files_and_says_orbit_serve_uses_them_after_a_restart(
    certs: CertsDir, config_dir: Path
) -> None:
    result = _certs(config_dir, "server", "orbit.example.com", "10.0.0.5")

    [old] = certs.path.glob("old-*")
    assert result.stdout == (
        f"Moved the old files to {old}.\n"
        "Wrote a server certificate for orbit.example.com, 10.0.0.5, localhost and 127.0.0.1:\n"
        f"  {certs.server}\n  {certs.server_key}\n\n"
        "Restart orbit serve for it to use them.\n"
    )


def test_server_without_names_makes_the_certificate_for_the_machines_hostname(
    certs: CertsDir, config_dir: Path
) -> None:
    result = _certs(config_dir, "server")

    assert result.exit_code == 0, result.output
    assert x509.DNSName(socket.gethostname()) in _alternative_names(certs.server)


def test_server_without_a_server_certificate_writes_one_setting_nothing_aside(
    certs: CertsDir, config_dir: Path
) -> None:
    certs.server.unlink()
    certs.server_key.unlink()
    result = _certs(config_dir, "server", "box")

    assert result.exit_code == 0, result.output
    assert not list(certs.path.glob("old-*"))
    assert result.stdout.startswith("Wrote a server certificate for box, localhost and")
    _certificate(certs.server).verify_directly_issued_by(_certificate(certs.ca))


def test_server_makes_the_key_type_and_lifetime_its_given(
    certs: CertsDir, config_dir: Path
) -> None:
    result = _certs(config_dir, "server", "box", "--key-type", "p256", "--days", "30")

    assert result.exit_code == 0, result.output
    key = load_pem_private_key(certs.server_key.read_bytes(), None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    certificate = _certificate(certs.server)
    assert certificate.not_valid_after_utc - certificate.not_valid_before_utc == timedelta(days=30)


def test_server_without_a_ca_is_refused_pointing_to_init(config_dir: Path) -> None:
    result = _certs(config_dir, "server", "box")

    certs = CertsDir(config_dir)
    assert result.exit_code == 2
    assert result.stderr == (
        f"There's no CA certificate at {certs.ca}; make a CA with orbit certs init.\n"
    )
    assert not certs.path.exists()


def test_server_without_the_cas_key_is_refused_naming_ca_key_leaving_the_server_alone(
    certs: CertsDir, config_dir: Path, tmp_path: Path
) -> None:
    certs.ca_key.rename(tmp_path / "offline.pem")
    before = _contents(certs.server, certs.server_key)
    result = _certs(config_dir, "server", "box")

    assert result.exit_code == 2
    assert result.stderr == (
        f"The CA's key isn't at {certs.ca_key}; pass --ca-key with where it is.\n"
    )
    assert _contents(certs.server, certs.server_key) == before
    assert not list(certs.path.glob("old-*"))


def test_server_signs_with_the_ca_key_where_its_given(
    certs: CertsDir, config_dir: Path, tmp_path: Path
) -> None:
    certs.ca_key.rename(tmp_path / "offline.pem")
    result = _certs(config_dir, "server", "box", "--ca-key", str(tmp_path / "offline.pem"))

    assert result.exit_code == 0, result.output
    _certificate(certs.server).verify_directly_issued_by(_certificate(certs.ca))
    assert not certs.ca_key.exists()


def test_a_name_that_isnt_a_hostname_is_refused_setting_nothing_aside(
    certs: CertsDir, config_dir: Path
) -> None:
    before = _contents(certs.server, certs.server_key)
    result = _certs(config_dir, "server", "two words")

    assert result.exit_code == 2
    assert result.stderr.startswith('Can\'t make a certificate for "two words"')
    assert _contents(certs.server, certs.server_key) == before
    assert not list(certs.path.glob("old-*"))
