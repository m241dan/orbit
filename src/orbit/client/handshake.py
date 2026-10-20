"""Which failures to open a connection say the control plane can't be reached safely as asked,
so retrying would only fail the same way, and what to tell the user for each."""

import ssl
from urllib.parse import urlsplit

from websockets.exceptions import InvalidMessage

from orbit.client import messages

# OpenSSL's verification errors for a certificate whose chain ends at no trusted CA.
_UNTRUSTED = frozenset({2, 18, 19, 20, 21})
# Its verification errors for a certificate that doesn't name the host or address.
_WRONG_NAME = frozenset({62, 64})
# Its errors on reading a plaintext answer as TLS, by OpenSSL version.
_PLAINTEXT = frozenset({"WRONG_VERSION_NUMBER", "RECORD_LAYER_FAILURE", "HTTP_REQUEST"})


def handshake_failure(url: str, error: Exception) -> str | None:
    """What to tell the user about ``error``, raised opening a connection to ``url``, if it
    would recur on retrying; none if it may pass."""
    if isinstance(error, ssl.SSLCertVerificationError):
        return _unverified(url, error)
    if isinstance(error, ssl.SSLError):
        return messages.NOT_TLS.format(url=url) if error.reason in _PLAINTEXT else None
    if _plaintext_to_tls(url, error):
        return messages.SPEAKS_TLS.format(url=url)
    return None


def _unverified(url: str, error: ssl.SSLCertVerificationError) -> str:
    """What to tell the user about the certificate at ``url`` failing verification with
    ``error``."""
    if error.verify_code in _WRONG_NAME:
        return messages.HOSTNAME_MISMATCH.format(url=url, host=urlsplit(url).hostname)
    if error.verify_code in _UNTRUSTED:
        return messages.UNKNOWN_CA.format(url=url)
    return messages.CERTIFICATE_INVALID.format(url=url, reason=error.verify_message)


def _plaintext_to_tls(url: str, error: Exception) -> bool:
    """Whether ``error`` is a ``ws://`` connection to ``url`` closed before any HTTP answer,
    as a server serving TLS closes one that doesn't start with a TLS handshake."""
    closed_unanswered = isinstance(error, InvalidMessage) and isinstance(error.__cause__, EOFError)
    return closed_unanswered and urlsplit(url).scheme == "ws"
