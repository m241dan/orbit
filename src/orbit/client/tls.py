"""How a frontend secures its connection: TLS that always verifies the control plane, and
plaintext only to this machine."""

import ipaddress
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from orbit.client import messages

_LOCALHOST = "localhost"


class CaUnreadable(Exception):
    """The CA file can't be used; the message names it and says why."""


class PlaintextRefused(Exception):
    """A ``ws://`` URL names a host beyond loopback; the message names it and the fix."""


def client_ssl_context(ca: Path | None) -> ssl.SSLContext:
    """A context that trusts the CA certificate in the file at ``ca``, or the system's trusted
    CAs without one, and always checks the certificate and the hostname it names.

    Raises :class:`CaUnreadable` if ``ca`` can't be read or holds no certificate.
    """
    try:
        return ssl.create_default_context(cafile=ca)
    except OSError as exc:  # ssl.SSLError among them, for a file that isn't PEM.
        reason = exc.strerror or str(exc)
        raise CaUnreadable(messages.CA_UNREADABLE.format(path=ca, reason=reason)) from None


def refuse_plaintext(url: str) -> None:
    """Raise :class:`PlaintextRefused` if ``url`` is a ``ws://`` URL to a host beyond
    loopback, which would carry the token unencrypted."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "ws" and not _is_loopback(host):
        raise PlaintextRefused(messages.PLAINTEXT_NOT_LOOPBACK.format(url=url, host=host))


def _is_loopback(host: str) -> bool:
    """Whether ``host`` is ``localhost`` or a loopback address, reachable from this machine
    alone."""
    if host.lower() == _LOCALHOST:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
