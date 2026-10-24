"""Test certificates: a private CA and the server and client certificates it signs, written as
PEM files."""

import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

DAY = timedelta(days=1)

# Python's strict verification wants a CA that says it signs certificates.
_CA_USAGE = x509.KeyUsage(
    digital_signature=False,
    content_commitment=False,
    key_encipherment=False,
    data_encipherment=False,
    key_agreement=False,
    key_cert_sign=True,
    crl_sign=True,
    encipher_only=False,
    decipher_only=False,
)


@dataclass(frozen=True)
class Ca:
    """A CA, its certificate at ``path``, that signs with ``key``."""

    path: Path
    certificate: x509.Certificate
    key: ec.EllipticCurvePrivateKey


@dataclass(frozen=True)
class ServerFiles:
    """A server or client certificate at ``cert`` and its private key at ``key``."""

    cert: Path
    key: Path


def _name(common_name: str) -> x509.Name:
    """The name with ``common_name`` alone."""
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _pem(path: Path, data: bytes) -> Path:
    """``path``, with ``data`` written to it readable by its owner alone."""
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    """``key`` as unencrypted PEM."""
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def make_ca(directory: Path) -> Ca:
    """A CA valid for a year from yesterday, its certificate written to ``ca.pem`` in
    ``directory``."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name("orbit test CA"))
        .issuer_name(_name("orbit test CA"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - DAY)
        .not_valid_after(now + 365 * DAY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(_CA_USAGE, critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    path = _pem(directory / "ca.pem", certificate.public_bytes(serialization.Encoding.PEM))
    return Ca(path, certificate, key)


def _alternative_name(host: str) -> x509.GeneralName:
    """``host`` as a subject alternative name: an address, else a DNS name."""
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def make_server(
    ca: Ca,
    directory: Path,
    valid_from: timedelta = -DAY,
    valid_for: timedelta = 90 * DAY,
    hosts: tuple[str, ...] = ("localhost", "127.0.0.1"),
) -> ServerFiles:
    """A certificate for ``hosts`` that ``ca`` signs, valid from ``valid_from`` after now for
    ``valid_for``, written with its key to ``server.pem`` and ``server-key.pem`` in
    ``directory``."""
    key = ec.generate_private_key(ec.SECP256R1())
    start = datetime.now(UTC) + valid_from
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name("localhost"))
        .issuer_name(ca.certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + valid_for)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.key.public_key()), critical=False
        )
    )
    if hosts:  # Without hosts, a certificate with no subject alternative names at all.
        names = [_alternative_name(host) for host in hosts]
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
    certificate = builder.sign(ca.key, hashes.SHA256())
    cert = _pem(directory / "server.pem", certificate.public_bytes(serialization.Encoding.PEM))
    return ServerFiles(cert, _pem(directory / "server-key.pem", _key_pem(key)))


def make_client(ca: Ca, directory: Path, name: str = "laptop") -> ServerFiles:
    """A client certificate for ``name`` that ``ca`` signs, valid for 90 days from yesterday,
    written with its key to ``<name>.pem`` and ``<name>-key.pem`` in ``directory``."""
    key = ec.generate_private_key(ec.SECP256R1())
    start = datetime.now(UTC) - DAY
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(name))
        .issuer_name(ca.certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + 90 * DAY)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.key.public_key()), critical=False
        )
        .sign(ca.key, hashes.SHA256())
    )
    cert = _pem(directory / f"{name}.pem", certificate.public_bytes(serialization.Encoding.PEM))
    return ServerFiles(cert, _pem(directory / f"{name}-key.pem", _key_pem(key)))


def other_key(directory: Path) -> Path:
    """A private key no certificate is made for, written to ``other-key.pem`` in
    ``directory``."""
    return _pem(directory / "other-key.pem", _key_pem(ec.generate_private_key(ec.SECP256R1())))


@pytest.fixture
def ca(tmp_path: Path) -> Ca:
    """A certificate authority in ``tmp_path``."""
    return make_ca(tmp_path)
