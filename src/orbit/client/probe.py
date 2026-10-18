"""Whether a control plane takes a frontend's token, asked with an upgrade that's closed at
once."""

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from orbit.client.session import UNAUTHORIZED

PROBE_SECONDS = 5
"""How long the upgrade may take before the control plane is taken as unreachable."""


class TokenRejected(Exception):
    """The control plane turned the token away."""


async def accepts(url: str, token: str) -> bool:
    """Whether the control plane at ``url`` takes a connection showing ``token``; ``False`` if
    it can't be reached.

    Raises :class:`TokenRejected` if it turns the token away.
    """
    try:
        async with connect(
            url,
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=PROBE_SECONDS,
            proxy=None,
        ):
            return True
    except InvalidStatus as error:
        if error.response.status_code == UNAUTHORIZED:
            raise TokenRejected() from None
        return False
    except (OSError, TimeoutError, WebSocketException):
        return False
