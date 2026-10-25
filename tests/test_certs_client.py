"""``orbit certs client``: a client certificate the CA signs, written to ``certs/clients`` in the
config directory, with its fingerprint."""

import re
import stat
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, load_pem_private_key
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from typer.testing import CliRunner, Result

from orbit.agent.domain.server import client_fingerprint
from orbit.agent.external.certificates import CertsDir
from orbit.cli import app

runner = CliRunner()


def _certs(config_dir: Path, *args: str) -> Result:
    return runner.invoke(app, ["certs", *args, "--config-dir", str(config_dir)])


def _certificate(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _fingerprint(path: Path) -> str:
    return client_fingerprint(_certificate(path).public_bytes(Encoding.DER))


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _pair(certs: CertsDir) -> list[Path]:
    return [certs.client("laptop"), certs.client_key("laptop")]


def _contents(*paths: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in paths}


@pytest.fixture
def certs(config_dir: Path) -> CertsDir:
    """The files of the CA and server certificate ``orbit certs init`` made in ``config_dir``."""
    result = _certs(config_dir, "init", "box")
    assert result.exit_code == 0, result.output
    return CertsDir(config_dir)


def test_client_writes_a_certificate_for_client_authentication_named_for_it(
    certs: CertsDir, config_dir: Path
) -> None:
    result = _certs(config_dir, "client", "laptop")

    assert result.exit_code == 0, result.output
    certificate = _certificate(certs.client("laptop"))
    [common_name] = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    assert common_name.value == "laptop"
    usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert list(usage.value) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    certificate.verify_directly_issued_by(_certificate(certs.ca))
    key = load_pem_private_key(certs.client_key("laptop").read_bytes(), None)
    assert key.public_key() == certificate.public_key()


def test_client_writes_the_key_owner_only(certs: CertsDir, config_dir: Path) -> None:
    _certs(config_dir, "client", "laptop")

    assert _mode(certs.client_key("laptop")) == 0o600
    assert _mode(certs.client("laptop")) == 0o644
    assert _mode(certs.path / "clients") == 0o700


def test_client_names_the_files_and_the_certificates_fingerprint(
    certs: CertsDir, config_dir: Path
) -> None:
    result = _certs(config_dir, "client", "laptop")

    fingerprint = _fingerprint(certs.client("laptop"))
    assert len(re.findall("[0-9A-F]", fingerprint)) == 64
    assert result.stdout == (
        f"Wrote a certificate for the client laptop, with the SHA-256 fingerprint {fingerprint}:\n"
        f"  {certs.client('laptop')}\n  {certs.client_key('laptop')}\n\n"
        f"Copy them and {certs.ca} to the client machine over a trusted channel, such as scp,"
        " for orbit chat's --cert, --key and --ca.\n"
    )


def test_a_second_client_of_the_name_is_refused_leaving_its_pair_alone(
    certs: CertsDir, config_dir: Path
) -> None:
    _certs(config_dir, "client", "laptop")
    before = _contents(*_pair(certs))
    result = _certs(config_dir, "client", "laptop")

    assert result.exit_code == 2
    assert result.stderr == (
        "A client certificate named laptop exists; use --force to issue a new one (the old one"
        " stays valid until you add its fingerprint to [server] revoked).\n"
    )
    assert _contents(*_pair(certs)) == before
    assert not list(certs.path.glob("old-*"))


def test_client_with_force_sets_the_old_pair_aside_and_says_to_revoke_it(
    certs: CertsDir, config_dir: Path
) -> None:
    _certs(config_dir, "client", "laptop")
    before = _contents(*_pair(certs))
    old_fingerprint = _fingerprint(certs.client("laptop"))
    result = _certs(config_dir, "client", "laptop", "--force")

    assert result.exit_code == 0, result.output
    [old] = certs.path.glob("old-*")
    assert _contents(*old.iterdir()) == before
    assert _fingerprint(certs.client("laptop")) != old_fingerprint
    assert result.stdout.startswith(f"Moved the old files to {old}.\nWrote a certificate")
    assert result.stdout.endswith(
        f"\n\nAdd {old_fingerprint} to [server] revoked to stop the old certificate working.\n"
    )


def test_client_sets_aside_a_key_left_without_its_certificate(
    certs: CertsDir, config_dir: Path
) -> None:
    _certs(config_dir, "client", "laptop")
    certs.client("laptop").unlink()
    key = certs.client_key("laptop").read_bytes()
    result = _certs(config_dir, "client", "laptop")

    assert result.exit_code == 0, result.output
    [old] = certs.path.glob("old-*")
    assert _contents(*old.iterdir()) == {"laptop-key.pem": key}
    assert "revoked" not in result.stdout


def test_force_over_a_certificate_that_cant_be_read_is_refused_moving_nothing(
    certs: CertsDir, config_dir: Path
) -> None:
    _certs(config_dir, "client", "laptop")
    certs.client("laptop").write_text("not a certificate")
    result = _certs(config_dir, "client", "laptop", "--force")

    assert result.exit_code == 2
    assert result.stderr.startswith(f"Can't read {certs.client('laptop')}: it isn't a certificate")
    assert not list(certs.path.glob("old-*"))


def test_client_makes_the_key_type_and_lifetime_its_given(
    certs: CertsDir, config_dir: Path
) -> None:
    result = _certs(config_dir, "client", "laptop", "--key-type", "p256", "--days", "30")

    assert result.exit_code == 0, result.output
    key = load_pem_private_key(certs.client_key("laptop").read_bytes(), None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    certificate = _certificate(certs.client("laptop"))
    assert certificate.not_valid_after_utc - certificate.not_valid_before_utc == timedelta(days=30)


def test_client_without_the_cas_key_is_refused_naming_ca_key(
    certs: CertsDir, config_dir: Path, tmp_path: Path
) -> None:
    certs.ca_key.rename(tmp_path / "offline.pem")
    result = _certs(config_dir, "client", "laptop")

    assert result.exit_code == 2
    assert result.stderr == (
        f"The CA's key isn't at {certs.ca_key}; pass --ca-key with where it is.\n"
    )
    assert not (certs.path / "clients").exists()


def test_client_signs_with_the_ca_key_where_its_given(
    certs: CertsDir, config_dir: Path, tmp_path: Path
) -> None:
    certs.ca_key.rename(tmp_path / "offline.pem")
    result = _certs(config_dir, "client", "laptop", "--ca-key", str(tmp_path / "offline.pem"))

    assert result.exit_code == 0, result.output
    _certificate(certs.client("laptop")).verify_directly_issued_by(_certificate(certs.ca))


def test_client_without_a_ca_is_refused_pointing_to_init(config_dir: Path) -> None:
    result = _certs(config_dir, "client", "laptop")

    certs = CertsDir(config_dir)
    assert result.exit_code == 2
    assert result.stderr == (
        f"There's no CA certificate at {certs.ca}; make a CA with orbit certs init.\n"
    )
    assert not certs.path.exists()


@pytest.mark.parametrize("name", ["../laptop", ".laptop", "lap/top", "laptop-key"])
def test_a_client_name_that_isnt_a_safe_file_name_is_refused_writing_nothing(
    certs: CertsDir, config_dir: Path, name: str
) -> None:
    result = _certs(config_dir, "client", name)

    assert result.exit_code == 2
    assert result.stderr.startswith(f'Can\'t name a client "{name}": its files are named for it')
    assert not (certs.path / "clients").exists()
    assert sorted(path.name for path in certs.path.parent.iterdir()) == ["certs"]


def test_a_client_named_for_anothers_key_is_refused_leaving_its_pair_alone(
    certs: CertsDir, config_dir: Path
) -> None:
    _certs(config_dir, "client", "laptop")
    before = {path: path.read_bytes() for path in certs.path.rglob("*") if path.is_file()}
    result = _certs(config_dir, "client", "laptop-key")

    assert result.exit_code == 2
    assert result.stderr.startswith('Can\'t name a client "laptop-key"')
    assert {path: path.read_bytes() for path in certs.path.rglob("*") if path.is_file()} == before
