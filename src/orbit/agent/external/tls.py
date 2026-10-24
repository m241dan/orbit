"""Whether ``orbit serve`` can serve as its settings say: plaintext on loopback alone, and over
TLS a private key readable by its owner alone, matching a certificate that's valid now, and
under mutual TLS a client CA that can be read; and how a frontend on this machine reaches it."""

import ssl
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from orbit.agent.domain.server import (
    CertificateDates,
    LocalTls,
    ServerSettings,
    certificate_dates,
    local_candidates,
    local_hostname,
    missing_tls_key,
)
from orbit.agent.external import tls_messages
from orbit.protocol.transport import is_loopback, too_open


class TlsError(Exception):
    """``orbit serve`` can't serve as its settings say; the message says why and the fix."""


class LocalDiscoveryUnavailable(Exception):
    """Frontends on this machine can't find the server on their own; the message says why and
    the fix."""


def tls_warnings(settings: ServerSettings, now: datetime | None = None) -> tuple[str, ...]:
    """What to warn of as ``orbit serve`` starts with ``settings`` at ``now`` (default: the
    present).

    Raises :class:`TlsError` if it can't serve as they say.
    """
    if not settings.secure:
        _check_plaintext(settings.host)
        return ()
    cert, key = _files(settings)
    if settings.mutual:
        assert settings.client_ca is not None  # As _files checked.
        _certificate(Path(settings.client_ca))
    _check_key(key)
    warnings = _check_dates(cert, now or datetime.now(UTC))
    _check_pair(cert, key)
    return warnings


def _check_plaintext(host: str) -> None:
    """Raises :class:`TlsError` if ``host`` isn't loopback, which plaintext is for alone."""
    if not is_loopback(host):
        raise TlsError(tls_messages.NOT_LOOPBACK.format(host=host))


def _files(settings: ServerSettings) -> tuple[Path, Path]:
    """The certificate and private key ``settings`` name.

    Raises :class:`TlsError` if their mode is missing one of its files.
    """
    missing = missing_tls_key(settings)
    if missing is not None:
        flag = missing.replace("_", "-")
        raise TlsError(tls_messages.MISSING_FILE.format(mode=settings.tls, key=missing, flag=flag))
    assert settings.cert is not None and settings.key is not None
    return Path(settings.cert), Path(settings.key)


def _check_key(path: Path) -> None:
    """Raises :class:`TlsError` if the private key at ``path`` can't be read, others can read
    it, or it isn't an unencrypted PEM private key."""
    try:
        mode = path.stat().st_mode
        data = path.read_bytes()
    except OSError as exc:
        raise TlsError(_unreadable(path, exc.strerror or str(exc))) from None
    if too_open(mode):
        raise TlsError(tls_messages.KEY_READABLE.format(path=path))
    try:
        load_pem_private_key(data, password=None)
    except (ValueError, TypeError):
        raise TlsError(_unreadable(path, "not an unencrypted PEM private key")) from None


def _check_dates(path: Path, now: datetime) -> tuple[str, ...]:
    """What to warn of for the certificate at ``path`` at ``now``.

    Raises :class:`TlsError` if it can't be read, isn't PEM, or isn't valid at ``now``.
    """
    certificate = _certificate(path)
    start, end = certificate.not_valid_before_utc, certificate.not_valid_after_utc
    dates = {"cert": path, "not_before": _date(start), "not_after": _date(end)}
    match certificate_dates(start, end, now):
        case CertificateDates.EXPIRED:
            raise TlsError(tls_messages.EXPIRED.format(**dates))
        case CertificateDates.NOT_YET_VALID:
            raise TlsError(tls_messages.NOT_YET_VALID.format(**dates))
        case CertificateDates.EXPIRING:
            return (tls_messages.EXPIRING.format(**dates),)
        case _:
            return ()


def _certificate(path: Path) -> x509.Certificate:
    """The PEM certificate at ``path``; raises :class:`TlsError` if it can't be read."""
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except OSError as exc:
        raise TlsError(_unreadable(path, exc.strerror or str(exc))) from None
    except ValueError:
        raise TlsError(_unreadable(path, "not a PEM certificate")) from None


def _check_pair(cert: Path, key: Path) -> None:
    """Raises :class:`TlsError` if ``key`` isn't the private key of ``cert``."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(cert, key)
    except ssl.SSLError:
        raise TlsError(tls_messages.KEY_MISMATCH.format(key=key, cert=cert)) from None


def _unreadable(path: Path, reason: str) -> str:
    """The message for a file at ``path`` that can't be read for ``reason``."""
    return tls_messages.UNREADABLE.format(path=path, reason=reason)


def _date(moment: datetime) -> str:
    """``moment`` to the minute, as messages show it."""
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def local_tls(settings: ServerSettings, base: Path) -> LocalTls:
    """How a frontend on this machine reaches the server ``settings`` serve with TLS, which
    :func:`tls_warnings` has checked: trusting ``ca``, else ``client_ca``, taken from the
    directory ``base`` if relative, by a name the certificate gives.

    Raises :class:`LocalDiscoveryUnavailable` if neither CA is set, or the certificate gives
    no name for this machine.
    """
    assert settings.cert is not None  # As tls_warnings checked.
    ca = settings.ca or settings.client_ca
    if ca is None:
        raise LocalDiscoveryUnavailable(tls_messages.LOCAL_NO_CA.format(cert=settings.cert))
    cert = Path(settings.cert)
    hostname = local_hostname(settings.host, _certificate_names(_certificate(cert)))
    if hostname is None:
        names = ", ".join(local_candidates(settings.host))
        message = tls_messages.LOCAL_NO_HOSTNAME.format(cert=cert, names=names)
        raise LocalDiscoveryUnavailable(message)
    return LocalTls(mutual=settings.mutual, ca=str(base / ca), hostname=hostname)


def _certificate_names(certificate: x509.Certificate) -> tuple[str, ...]:
    """The hostnames and addresses ``certificate`` gives as subject alternative names."""
    try:
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return ()
    addresses = names.get_values_for_type(x509.IPAddress)
    return (*names.get_values_for_type(x509.DNSName), *(str(a) for a in addresses))
