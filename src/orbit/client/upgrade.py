"""Opening a WebSocket to a control plane, showing a frontend's token on the upgrade."""

from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus, WebSocketException

from orbit.protocol import UNAUTHORIZED, bearer_header


class TokenRejected(Exception):
    """The control plane turned the token away."""


async def open_connection(url: str, token: str, **options: Any) -> ClientConnection | None:
    """A connection to the control plane at ``url`` showing ``token``, opened with the other
    ``options`` websockets' ``connect`` takes, or none if it can't be made.

    Raises :class:`TokenRejected` if the control plane turns the token away.
    """
    try:
        return await connect(url, additional_headers=bearer_header(token), proxy=None, **options)
    except InvalidStatus as error:
        if error.response.status_code == UNAUTHORIZED:
            raise TokenRejected() from None
        return None
    except (OSError, TimeoutError, WebSocketException):
        return None
