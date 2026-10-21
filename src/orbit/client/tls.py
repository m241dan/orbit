"""How a frontend secures its connection: TLS that always verifies the control plane, showing
a client certificate if it has one, and plaintext only to this machine."""

import ssl
from pathlib import Path
from urllib.parse import urlsplit

from orbit.client import messages
from orbit.protocol.transport import is_loopback, too_open


class CaUnreadable(Exception):
    """The CA file can't be used; the message names it and says why."""


class ClientCertUnusable(Exception):
    """The client certificate or its key can't be used; the message names them and says
    why."""


class PlaintextRefused(Exception):
    """A ``ws://`` URL names a host beyond loopback; the message names it and the fix."""


def client_ssl_context(
    ca: Path | None, cert: Path | None = None, key: Path | None = None
) -> ssl.SSLContext:
    """A context that trusts the CA certificate in the file at ``ca``, or the system's trusted
    CAs without one, always checks the certificate and the hostname it names, and shows the
    client certificate at ``cert``, with its private key at ``key``, if they're given.

    Raises :class:`CaUnreadable` if ``ca`` can't be read or holds no certificate, and
    :class:`ClientCertUnusable` if only one of ``cert`` and ``key`` is given, others can read
    the key, or they can't be read or don't match.
    """
    try:
        context = ssl.create_default_context(cafile=ca)
    except OSError as exc:  # ssl.SSLError among them, for a file that isn't PEM.
        reason = exc.strerror or str(exc)
        raise CaUnreadable(messages.CA_UNREADABLE.format(path=ca, reason=reason)) from None
    if cert is not None or key is not None:
        _load_client_certificate(context, cert, key)
    return context


def _load_client_certificate(context: ssl.SSLContext, cert: Path | None, key: Path | None) -> None:
    """Have ``context`` show the certificate at ``cert`` with its private key at ``key``.

    Raises :class:`ClientCertUnusable` if one of them is missing, others can read the key, or
    they can't be read or don't match.
    """
    if cert is None or key is None:
        given = "--cert" if key is None else "--key"
        raise ClientCertUnusable(messages.CERT_WITHOUT_KEY.format(given=given))
    try:
        _check_key(key)
        context.load_cert_chain(cert, key)
    except OSError as exc:  # ssl.SSLError among them, for files that aren't PEM or don't match.
        reason = exc.strerror or str(exc)
        message = messages.CLIENT_CERT_UNREADABLE.format(cert=cert, key=key, reason=reason)
        raise ClientCertUnusable(message) from None


def _check_key(path: Path) -> None:
    """Raises :class:`ClientCertUnusable` if others can read the private key at ``path``, and
    ``OSError`` if it can't be found."""
    if too_open(path.stat().st_mode):
        raise ClientCertUnusable(messages.KEY_READABLE.format(path=path))


def refuse_plaintext(url: str) -> None:
    """Raise :class:`PlaintextRefused` if ``url`` is a ``ws://`` URL to a host beyond
    loopback, which would carry the token unencrypted."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "ws" and not is_loopback(host):
        raise PlaintextRefused(messages.PLAINTEXT_NOT_LOOPBACK.format(url=url, host=host))
