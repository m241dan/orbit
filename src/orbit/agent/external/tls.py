"""Whether ``orbit serve`` can serve as its settings say: plaintext on loopback alone, and over
TLS a private key readable by its owner alone, matching a certificate that's valid now."""

import ssl
import stat
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from orbit.agent.domain.server import (
    CertificateDates,
    ServerSettings,
    certificate_dates,
    is_loopback,
    missing_tls_key,
)
from orbit.agent.external import tls_messages


class TlsError(Exception):
    """``orbit serve`` can't serve as its settings say; the message says why and the fix."""


def tls_warnings(settings: ServerSettings, now: datetime | None = None) -> tuple[str, ...]:
    """What to warn of as ``orbit serve`` starts with ``settings`` at ``now`` (default: the
    present).

    Raises :class:`TlsError` if it can't serve as they say.
    """
    if settings.tls == "off":
        _check_plaintext(settings.host)
        return ()
    cert, key = _files(settings)
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

    Raises :class:`TlsError` if their mode is missing one of its files, or is mutual TLS.
    """
    missing = missing_tls_key(settings)
    if missing is not None:
        flag = missing.replace("_", "-")
        raise TlsError(tls_messages.MISSING_FILE.format(mode=settings.tls, key=missing, flag=flag))
    if settings.tls == "mtls":
        raise TlsError(tls_messages.MTLS_NOT_SUPPORTED)
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
    if stat.S_IMODE(mode) & 0o077:
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
