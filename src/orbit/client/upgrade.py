"""Opening a WebSocket to a control plane, showing a frontend's token on the upgrade."""

import ssl
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus, WebSocketException

from orbit.client.handshake import handshake_failure
from orbit.protocol import UNAUTHORIZED, bearer_header


class TokenRejected(Exception):
    """The control plane turned the token away."""


class HandshakeRefused(Exception):
    """The connection can't be made safely as asked, and retrying would fail the same way; the
    message says why and the fix."""


async def open_connection(
    url: str, token: str, ssl_context: ssl.SSLContext | None = None, **options: Any
) -> ClientConnection | None:
    """A connection to the control plane at ``url`` showing ``token``, verified with
    ``ssl_context``, if given, for a ``wss://`` URL, opened with the other ``options``
    websockets' ``connect`` takes, or none if it can't be made.

    Raises :class:`TokenRejected` if the control plane turns the token away, and
    :class:`HandshakeRefused` if the connection fails in a way that would recur.
    """
    options.update(_tls(url, ssl_context))
    try:
        return await connect(url, additional_headers=bearer_header(token), proxy=None, **options)
    except InvalidStatus as error:
        if error.response.status_code == UNAUTHORIZED:
            raise TokenRejected() from None
        return None
    except (OSError, TimeoutError, WebSocketException) as error:
        message = handshake_failure(url, error)
        if message is not None:
            raise HandshakeRefused(message) from None
        return None


def _tls(url: str, ssl_context: ssl.SSLContext | None) -> dict[str, ssl.SSLContext]:
    """The option verifying the control plane at ``url`` with ``ssl_context``: none for a
    ``ws://`` URL, which has no TLS, or without a context, websockets' default verifying with
    the system's trusted CAs."""
    if ssl_context is not None and url.startswith("wss://"):
        return {"ssl": ssl_context}
    return {}
