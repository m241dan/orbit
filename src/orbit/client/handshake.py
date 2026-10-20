"""Which failures to open a connection say the control plane can't be reached safely as asked,
so retrying would only fail the same way, and what to tell the user for each.

A server under mutual TLS that refuses a client's certificate in the TLS handshake drops the
connection without saying why, so the client can only tell, by whether it showed one, which it
likely was."""

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


def handshake_failure(url: str, error: Exception, certified: bool = False) -> str | None:
    """What to tell the user about ``error``, raised opening a connection to ``url``, showing a
    client certificate if ``certified``, if it would recur on retrying; none if it may pass."""
    if isinstance(error, ssl.SSLCertVerificationError):
        return _unverified(url, error)
    if isinstance(error, ssl.SSLError):
        return messages.NOT_TLS.format(url=url) if error.reason in _PLAINTEXT else None
    return _unanswered(url, certified) if _closed_unanswered(error) else None


def _unverified(url: str, error: ssl.SSLCertVerificationError) -> str:
    """What to tell the user about the certificate at ``url`` failing verification with
    ``error``."""
    if error.verify_code in _WRONG_NAME:
        return messages.HOSTNAME_MISMATCH.format(url=url, host=urlsplit(url).hostname)
    if error.verify_code in _UNTRUSTED:
        return messages.UNKNOWN_CA.format(url=url)
    return messages.CERTIFICATE_INVALID.format(url=url, reason=error.verify_message)


def _unanswered(url: str, certified: bool) -> str:
    """What to tell the user about ``url`` closing the connection unanswered, showing a client
    certificate if ``certified``."""
    if urlsplit(url).scheme == "ws":
        return messages.SPEAKS_TLS.format(url=url)
    refused = messages.CLIENT_CERT_REJECTED if certified else messages.CLIENT_CERT_REQUIRED
    return refused.format(url=url)


def _closed_unanswered(error: Exception) -> bool:
    """Whether ``error`` is a connection closed before any HTTP answer: over ``ws://``, as a
    server serving TLS closes one that doesn't start with a TLS handshake, and over
    ``wss://``, as one under mutual TLS closes one showing a certificate its CA didn't sign."""
    return isinstance(error, InvalidMessage) and isinstance(error.__cause__, EOFError)
