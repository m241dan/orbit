"""orbit's own private CA, the server and client certificates it signs, the files in ``certs``
in the config directory that hold them, and the bundles that carry a client's to it."""

import io
import ipaddress
import os
import re
import tarfile
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from orbit.agent.domain.certificate_list import CertificateKind, ListedCertificate
from orbit.agent.domain.client_bundle import BundledFile
from orbit.agent.domain.server import client_fingerprint
from orbit.agent.external.whole_files import PRIVATE, replacing, write_whole

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
NO_SERVER = (
    "There's no server certificate at {path} to name the server in the bundle's URL; pass --url"
    " with the URL clients connect to, or make one with orbit certs server."
)
CANT_WRITE_BUNDLE = "Can't write {path}: {reason}. Check that you can write to {directory}."
NOT_A_CLIENT_NAME = (
    'Can\'t name a client "{name}": its files are named for it, so give up to 64 letters,'
    " digits, dots, dashes and underscores, starting with a letter or digit and not ending in"
    " -key, such as laptop; laptop-key.pem is laptop's key, so a client named laptop-key would"
    " share it."
)

# A hostname is labels of letters, digits, dashes and underscores, not starting or ending with a
# dash, the first of them maybe a wildcard, then maybe a dot: one a client can match.
_LABEL = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]*[A-Za-z0-9_])?"
_HOSTNAME = re.compile(rf"(?:\*\.)?{_LABEL}(?:\.{_LABEL})*\.?")
# X.509 holds a common name to 64 bytes in UTF-8; a longer one is cut short and ends with this.
_COMMON_NAME_BYTES = 64
_CUT = "…"
# A client's name is a file name, and its certificate's common name, which can't exceed 64. It
# can't end in -key, in any case, or its certificate's file would be another client's key's.
_CLIENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?<!-[Kk][Ee][Yy])")

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
    name = _name(_ca_name(hostname))
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


def certificate_pem(certificate: x509.Certificate) -> str:
    """``certificate`` in PEM."""
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def key_pem(key: PrivateKey) -> str:
    """``key`` in PEM, unencrypted."""
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem.decode("ascii")


def write_bundle(path: Path, files: Iterable[BundledFile]) -> None:
    """Write a tar of ``files`` to ``path``, whole or not at all, readable by its owner alone,
    each file in it readable by anyone unless it's private.

    Raises :class:`CertificateError` if it can't be written.
    """
    mtime = int(datetime.now(UTC).timestamp())
    try:
        with replacing(path, PRIVATE) as temporary, temporary.open("wb") as tar:
            with tarfile.open(fileobj=tar, mode="w") as archive:
                for file in files:
                    _add(archive, file, mtime)
            tar.flush()
            os.fsync(tar.fileno())
    except OSError as exc:
        reason = exc.strerror or str(exc)
        directory = path.absolute().parent
        message = CANT_WRITE_BUNDLE.format(path=path, reason=reason, directory=directory)
        raise CertificateError(message) from None


def _add(archive: tarfile.TarFile, file: BundledFile, mtime: int) -> None:
    """Add ``file`` to ``archive``, modified at ``mtime``, with its mode."""
    data = file.text.encode("utf-8")
    member = tarfile.TarInfo(file.name)
    member.size = len(data)
    member.mode = PRIVATE if file.private else PUBLIC
    member.mtime = mtime
    archive.addfile(member, io.BytesIO(data))


def read_certificate(path: Path) -> x509.Certificate:
    """The certificate in PEM at ``path``.

    Raises :class:`CertificateError` if it can't be read or isn't a certificate in PEM.
    """
    try:
        return x509.load_pem_x509_certificate(_read(path))
    except ValueError:
        raise CertificateError(NOT_A_CERTIFICATE.format(path=path)) from None


def _listed(kind: CertificateKind, path: Path) -> ListedCertificate:
    """The certificate of ``kind`` at ``path`` as ``orbit certs list`` shows it: a client's
    named by its file, else by its common name.

    Raises :class:`CertificateError` if it can't be read or isn't a certificate in PEM.
    """
    certificate = read_certificate(path)
    return ListedCertificate(
        kind=kind,
        name=path.stem if kind == "client" else _common_name(certificate),
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
        fingerprint=fingerprint(certificate),
        names=_alternative_names(certificate),
    )


def _common_name(certificate: x509.Certificate) -> str:
    """The common name of ``certificate``'s subject; its whole subject, as RFC 4514 writes it,
    if it has none."""
    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if not common_names:
        return certificate.subject.rfc4514_string()
    return str(common_names[0].value)


def _alternative_names(certificate: x509.Certificate) -> tuple[str, ...]:
    """The hostnames and addresses ``certificate`` holds as subject alternative names, in
    order."""
    try:
        extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return ()
    return tuple(str(name.value) for name in extension.value)


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


def _ca_name(hostname: str) -> str:
    """The common name of a CA of the machine ``hostname``: ``orbit CA (<hostname>)``, with as
    much of ``hostname`` as fits."""
    room = _COMMON_NAME_BYTES - len("orbit CA ()")
    return f"orbit CA ({_shortened(hostname, room)})"


def _name(common_name: str) -> x509.Name:
    """The name with ``common_name`` alone, as much of it as a common name holds."""
    shortened = _shortened(common_name, _COMMON_NAME_BYTES)
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, shortened)])


def _shortened(text: str, room: int) -> str:
    """``text`` if it takes at most ``room`` bytes in UTF-8, else as much of it as fits in them
    before ``…``, cut between letters."""
    encoded = text.encode()
    if len(encoded) <= room:
        return text
    kept = encoded[: room - len(_CUT.encode())]
    return kept.decode(errors="ignore") + _CUT  # Ignored: a letter cut in two.


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

    def listed(self) -> tuple[ListedCertificate, ...]:
        """The certificates in ``certs``, not those set aside: the CA's, the server's and each
        client's in ``clients`` by name, those there are, as ``orbit certs list`` shows them.

        Raises :class:`CertificateError` if one can't be read or isn't a certificate in PEM.
        """
        found: list[tuple[CertificateKind, Path]] = [("CA", self.ca), ("server", self.server)]
        found += [("client", path) for path in self._client_certificates()]
        return tuple(_listed(kind, path) for kind, path in found if path.exists())

    def _client_certificates(self) -> list[Path]:
        """The certificates in ``clients``, by name: its files but the keys."""
        files = (self.path / "clients").glob("*.pem")
        return sorted(path for path in files if not path.name.endswith("-key.pem"))

    def server_name(self) -> str:
        """The first name the server certificate in ``certs`` was made for: the first it holds,
        whole where its common name may be cut short, else its common name.

        Raises :class:`CertificateError` if there's none, or it can't be read or isn't a
        certificate in PEM.
        """
        if not self.server.exists():
            raise CertificateError(NO_SERVER.format(path=self.server))
        certificate = read_certificate(self.server)
        names = _alternative_names(certificate)
        return names[0] if names else _common_name(certificate)

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
        self._write(path, certificate_pem(certificate), PUBLIC)

    def write_key(self, path: Path, key: PrivateKey) -> None:
        """Write ``key`` to ``path``, unencrypted, whole or not at all, readable by its owner
        alone.

        Raises :class:`CertificateError` if it can't be written.
        """
        self._write(path, key_pem(key), PRIVATE)

    def _write(self, path: Path, pem: str, mode: int) -> None:
        """Write ``pem`` to ``path``, in ``certs``, with ``mode``, making its directory, and
        ``certs``, owner-only if they have to be made."""
        try:
            self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.parent.mkdir(mode=0o700, exist_ok=True)
            write_whole(path, pem, mode)
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
