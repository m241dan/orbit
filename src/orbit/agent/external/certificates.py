"""orbit's own private CA, the server and client certificates it signs, and the files in
``certs`` in the config directory that hold them."""

import ipaddress
import os
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from orbit.agent.domain.server import client_fingerprint
from orbit.agent.external.whole_files import PRIVATE, write_whole

KeyType = Literal["ed25519", "p256"]
"""The kind of key a certificate is made for: Ed25519, or ECDSA on P-256 for a browser or load
balancer in front that needs it."""

type PrivateKey = ed25519.Ed25519PrivateKey | ec.EllipticCurvePrivateKey

CA_DAYS = 3650
"""How long a CA is valid for: 10 years."""

CERTIFICATE_DAYS = 365
"""How long a server or client certificate is valid for unless told otherwise: a year."""

LOCAL_NAMES = ("localhost", "127.0.0.1")
"""The names every server certificate holds, so frontends on its machine can reach it."""

PUBLIC = 0o644  # A certificate: readable by anyone, writable by its owner alone.

NOT_A_NAME = (
    "Can't make a certificate for \"{name}\": it's neither a hostname nor an IP address. Give"
    " names such as orbit.example.com or 10.0.0.5, an internationalized name in its xn-- form."
)
CANT_WRITE = (
    "Can't write {path}: {reason}. Check that you can write to {directory}, or give another"
    " config directory with --config-dir."
)
CANT_MAKE = "Can't make {path}: {reason}. Check that you can write to {directory}."
CANT_MOVE = "Can't move {path} to {old}: {reason}. Check that you can write to {directory}."
CANT_READ = "Can't read {path}: {reason}. Check that you can read it."
NOT_A_CERTIFICATE = (
    "Can't read {path}: it isn't a certificate in PEM. Put back the one orbit certs wrote there,"
    " or move it away."
)
NOT_A_CA_KEY = (
    "Can't read {path}: it isn't an Ed25519 or ECDSA private key in PEM without a passphrase."
    " Pass --ca-key with the CA's key in that form."
)
NOT_THE_CA_KEY = "{key} isn't the key of the CA at {ca}; pass --ca-key with the CA's own key."
NO_CA = "There's no CA certificate at {path}; make a CA with orbit certs init."
NO_CA_KEY = "The CA's key isn't at {path}; pass --ca-key with where it is."
NOT_A_CLIENT_NAME = (
    'Can\'t name a client "{name}": its files are named for it, so give up to 64 letters,'
    " digits, dots, dashes and underscores, starting with a letter or digit, such as laptop."
)

_HOSTNAME = re.compile(r"[A-Za-z0-9_*.-]+")
# A client's name is a file name, and its certificate's common name, which can't exceed 64.
_CLIENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# Python's strict verification wants a CA that says it signs certificates, and a certificate
# that says what its key is for.
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
_SIGNING_USAGE = x509.KeyUsage(
    digital_signature=True,
    content_commitment=False,
    key_encipherment=False,
    data_encipherment=False,
    key_agreement=False,
    key_cert_sign=False,
    crl_sign=False,
    encipher_only=False,
    decipher_only=False,
)


class CertificateError(Exception):
    """A certificate can't be made or its files written; the message says why and the fix."""


def make_ca(
    hostname: str, key_type: KeyType = "ed25519", days: int = CA_DAYS
) -> tuple[x509.Certificate, PrivateKey]:
    """A new CA of the machine ``hostname``, with a ``key_type`` key, valid from now for
    ``days``, and its key."""
    key = _new_key(key_type)
    name = _name(f"orbit CA ({hostname})")
    certificate = (
        _builder(name, name, key, days)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_CA_USAGE, critical=True)
        .sign(key, _hash(key))
    )
    return certificate, key


def issue_server(
    ca: x509.Certificate,
    ca_key: PrivateKey,
    names: Sequence[str],
    key_type: KeyType = "ed25519",
    days: int = CERTIFICATE_DAYS,
) -> tuple[x509.Certificate, PrivateKey]:
    """A new certificate for server authentication, named for the first of ``names`` and holding
    :func:`server_names` of them, with a ``key_type`` key, that ``ca`` signs with ``ca_key``,
    valid from now for ``days``, and its key.

    Raises :class:`CertificateError` if one of ``names`` is neither a hostname nor an IP address.
    """
    alternatives = x509.SubjectAlternativeName([_alternative(n) for n in server_names(names)])
    key = _new_key(key_type)
    certificate = (
        _leaf(ca, ca_key, key, names[0], days)
        .add_extension(alternatives, critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, _hash(ca_key))
    )
    return certificate, key


def issue_client(
    ca: x509.Certificate,
    ca_key: PrivateKey,
    name: str,
    key_type: KeyType = "ed25519",
    days: int = CERTIFICATE_DAYS,
) -> tuple[x509.Certificate, PrivateKey]:
    """A new certificate for client authentication, named ``name``, with a ``key_type`` key,
    that ``ca`` signs with ``ca_key``, valid from now for ``days``, and its key."""
    key = _new_key(key_type)
    certificate = (
        _leaf(ca, ca_key, key, name, days)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(ca_key, _hash(ca_key))
    )
    return certificate, key


def fingerprint(certificate: x509.Certificate) -> str:
    """The SHA-256 fingerprint of ``certificate``, as ``orbit serve`` shows it and ``[server]
    revoked`` takes it."""
    return client_fingerprint(certificate.public_bytes(serialization.Encoding.DER))


def read_certificate(path: Path) -> x509.Certificate:
    """The certificate in PEM at ``path``.

    Raises :class:`CertificateError` if it can't be read or isn't a certificate in PEM.
    """
    try:
        return x509.load_pem_x509_certificate(_read(path))
    except ValueError:
        raise CertificateError(NOT_A_CERTIFICATE.format(path=path)) from None


def _read_ca_key(path: Path) -> PrivateKey:
    """The private key in PEM at ``path``, unencrypted, that a CA signs with.

    Raises :class:`CertificateError` if it can't be read or isn't such a key.
    """
    pem = _read(path)
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except ValueError, TypeError:  # TypeError: it's encrypted.
        raise CertificateError(NOT_A_CA_KEY.format(path=path)) from None
    if not isinstance(key, ed25519.Ed25519PrivateKey | ec.EllipticCurvePrivateKey):
        raise CertificateError(NOT_A_CA_KEY.format(path=path))
    return key


def _read(path: Path) -> bytes:
    """What's in the file at ``path``; raises :class:`CertificateError` if it can't be read."""
    try:
        return path.read_bytes()
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise CertificateError(CANT_READ.format(path=path, reason=reason)) from None


def server_names(names: Iterable[str]) -> tuple[str, ...]:
    """The names a server certificate for ``names`` holds: each of them once, then
    ``localhost`` and ``127.0.0.1`` unless among them."""
    return tuple(dict.fromkeys((*names, *LOCAL_NAMES)))


def _alternative(name: str) -> x509.GeneralName:
    """``name`` as a subject alternative name: an IP address, else a DNS name.

    Raises :class:`CertificateError` if it's neither a hostname nor an IP address.
    """
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        pass
    if not _HOSTNAME.fullmatch(name):
        raise CertificateError(NOT_A_NAME.format(name=name))
    return x509.DNSName(name)


def _new_key(key_type: KeyType) -> PrivateKey:
    """A new private key of ``key_type``."""
    if key_type == "p256":
        return ec.generate_private_key(ec.SECP256R1())
    return ed25519.Ed25519PrivateKey.generate()


def _hash(key: PrivateKey) -> hashes.SHA256 | None:
    """The hash a signature by ``key`` takes: none for Ed25519, which hashes for itself."""
    return None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256()


def _name(common_name: str) -> x509.Name:
    """The name with ``common_name`` alone."""
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _builder(
    subject: x509.Name, issuer: x509.Name, key: PrivateKey, days: int
) -> x509.CertificateBuilder:
    """A certificate of ``subject``'s ``key`` that ``issuer`` signs, with a random serial
    number, valid from now for ``days``."""
    now = datetime.now(UTC).replace(microsecond=0)
    public_key = key.public_key()
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
    )


def _leaf(
    ca: x509.Certificate, ca_key: PrivateKey, key: PrivateKey, common_name: str, days: int
) -> x509.CertificateBuilder:
    """A certificate, not a CA's, of ``key`` named ``common_name`` that ``ca`` signs with
    ``ca_key``, valid from now for ``days``."""
    issuer = x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key())
    return (
        _builder(_name(common_name), ca.subject, key, days)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_SIGNING_USAGE, critical=True)
        .add_extension(issuer, critical=False)
    )


class CertsDir:
    """The certificates and keys in ``certs`` in the config directory ``config_dir``: the CA's,
    the server's and each client's, in PEM."""

    def __init__(self, config_dir: Path) -> None:
        self.path = config_dir / "certs"
        self.ca = self.path / "ca.pem"
        self.ca_key = self.path / "ca-key.pem"
        self.server = self.path / "server.pem"
        self.server_key = self.path / "server-key.pem"

    def client(self, name: str) -> Path:
        """Where the certificate of the client ``name`` is.

        Raises :class:`CertificateError` if ``name`` isn't a safe file name.
        """
        return self._client_file(name, f"{name}.pem")

    def client_key(self, name: str) -> Path:
        """Where the private key of the client ``name`` is.

        Raises :class:`CertificateError` if ``name`` isn't a safe file name.
        """
        return self._client_file(name, f"{name}-key.pem")

    def _client_file(self, name: str, file_name: str) -> Path:
        """The file ``file_name`` in ``clients``, of the client ``name``; raises
        :class:`CertificateError` if ``name`` isn't a safe file name."""
        if not _CLIENT_NAME.fullmatch(name):
            raise CertificateError(NOT_A_CLIENT_NAME.format(name=name))
        return self.path / "clients" / file_name

    def issuer(self, key: Path | None = None) -> tuple[x509.Certificate, PrivateKey]:
        """The CA in ``certs`` and its key, read from ``key`` (default: ``ca-key.pem`` in
        ``certs``), which may start with ``~``.

        Raises :class:`CertificateError` if either is missing or can't be read, or the key isn't
        the CA's.
        """
        key = (key or self.ca_key).expanduser()
        if not self.ca.exists():
            raise CertificateError(NO_CA.format(path=self.ca))
        if not key.exists():
            raise CertificateError(NO_CA_KEY.format(path=key))
        ca = read_certificate(self.ca)
        ca_key = _read_ca_key(key)
        if ca_key.public_key() != ca.public_key():
            raise CertificateError(NOT_THE_CA_KEY.format(key=key, ca=self.ca))
        return ca, ca_key

    def write(self, path: Path, certificate: x509.Certificate) -> None:
        """Write ``certificate`` to ``path``, whole or not at all, readable by anyone.

        Raises :class:`CertificateError` if it can't be written.
        """
        self._write(path, certificate.public_bytes(serialization.Encoding.PEM), PUBLIC)

    def write_key(self, path: Path, key: PrivateKey) -> None:
        """Write ``key`` to ``path``, unencrypted, whole or not at all, readable by its owner
        alone.

        Raises :class:`CertificateError` if it can't be written.
        """
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self._write(path, pem, PRIVATE)

    def _write(self, path: Path, pem: bytes, mode: int) -> None:
        """Write ``pem`` to ``path``, in ``certs``, with ``mode``, making its directory, and
        ``certs``, owner-only if they have to be made."""
        try:
            self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.parent.mkdir(mode=0o700, exist_ok=True)
            write_whole(path, pem.decode("ascii"), mode)
        except OSError as exc:
            reason = exc.strerror or str(exc)
            message = CANT_WRITE.format(path=path, reason=reason, directory=self.path)
            raise CertificateError(message) from None

    def set_aside(self, paths: Iterable[Path], now: datetime | None = None) -> Path:
        """The new directory ``old-<UTC timestamp>`` in ``certs``, of ``now`` (default: the
        present) and made owner-only, that the files at ``paths`` are moved to.

        Raises :class:`CertificateError` if it can't be made or a file can't be moved.
        """
        old = self._old_directory(now or datetime.now(UTC))
        for path in paths:
            try:
                os.replace(path, old / path.name)
            except OSError as exc:
                reason = exc.strerror or str(exc)
                message = CANT_MOVE.format(path=path, old=old, reason=reason, directory=self.path)
                raise CertificateError(message) from None
        return old

    def _old_directory(self, now: datetime) -> Path:
        """A new, owner-only directory in ``certs`` named for ``now``, numbered if one already
        is; raises :class:`CertificateError` if it can't be made."""
        stamp = f"old-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}"
        count = 1
        while True:
            old = self.path / (stamp if count == 1 else f"{stamp}-{count}")
            try:
                old.mkdir(mode=0o700)
                return old
            except FileExistsError:
                count += 1
            except OSError as exc:
                reason = exc.strerror or str(exc)
                message = CANT_MAKE.format(path=old, reason=reason, directory=self.path)
                raise CertificateError(message) from None
