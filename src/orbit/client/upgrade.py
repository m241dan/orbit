"""Opening a WebSocket to a control plane, showing a frontend's token on the upgrade, or under
mutual TLS its client certificate in the TLS handshake, which the upgrade then checks."""

import ssl
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.http11 import Response

from orbit.client import messages
from orbit.client.handshake import handshake_failure
from orbit.protocol import CERTIFICATE_CHALLENGE, UNAUTHORIZED, bearer_header

_FORBIDDEN = 403  # As a control plane under mutual TLS refuses a revoked certificate.


class TokenRejected(Exception):
    """The control plane turned the token away."""


class HandshakeRefused(Exception):
    """The connection can't be made safely as asked, and retrying would fail the same way; the
    message says why and the fix."""


async def open_connection(
    url: str, token: str | None, ssl_context: ssl.SSLContext | None = None, **options: Any
) -> ClientConnection | None:
    """A connection to the control plane at ``url`` showing ``token``, or without one the
    client certificate in ``ssl_context``, verified with ``ssl_context``, if given, for a
    ``wss://`` URL, opened with the other ``options`` websockets' ``connect`` takes, or none if
    it can't be made.

    Raises :class:`TokenRejected` if the control plane turns the token away, and
    :class:`HandshakeRefused` if the connection fails in a way that would recur.
    """
    options.update(_tls(url, ssl_context))
    headers = {} if token is None else bearer_header(token)
    try:
        return await connect(url, additional_headers=headers, proxy=None, **options)
    except InvalidStatus as error:
        _check_refusal(url, error.response, token)
        return None
    except (OSError, TimeoutError, WebSocketException) as error:
        message = handshake_failure(url, error, certified=token is None)
        if message is not None:
            raise HandshakeRefused(message) from None
        return None


def _check_refusal(url: str, response: Response, token: str | None) -> None:
    """Note that the control plane at ``url`` answered the upgrade with ``response``, the
    client showing ``token``, or without one a client certificate.

    Raises :class:`TokenRejected` if it turned the token away, and :class:`HandshakeRefused`
    if it wants a token, not a certificate, or a certificate the client didn't show, or the
    certificate is revoked.
    """
    if response.status_code == _FORBIDDEN:
        raise HandshakeRefused(messages.REVOKED.format(url=url))
    if response.status_code != UNAUTHORIZED:
        return
    if response.headers.get("WWW-Authenticate") == CERTIFICATE_CHALLENGE:
        raise HandshakeRefused(messages.CLIENT_CERT_REQUIRED.format(url=url))
    if token is None:
        raise HandshakeRefused(messages.TOKEN_EXPECTED.format(url=url))
    raise TokenRejected()


def _tls(url: str, ssl_context: ssl.SSLContext | None) -> dict[str, ssl.SSLContext]:
    """The option verifying the control plane at ``url`` with ``ssl_context``: none for a
    ``ws://`` URL, which has no TLS, or without a context, websockets' default verifying with
    the system's trusted CAs."""
    if ssl_context is not None and url.startswith("wss://"):
        return {"ssl": ssl_context}
    return {}
