"""Whether a control plane takes a frontend's token, or client certificate, asked with an upgrade
that's closed at once."""

import ssl

from orbit.client.upgrade import HandshakeRefused, open_connection

PROBE_SECONDS = 5
"""How long the upgrade may take before the control plane is taken as unreachable."""


async def accepts(url: str, token: str | None, ssl_context: ssl.SSLContext | None = None) -> bool:
    """Whether the control plane at ``url`` takes a connection showing ``token``, or without
    one the client certificate in ``ssl_context``, verified with ``ssl_context`` if given;
    ``False`` if it can't be reached, safely or at all.

    Raises :class:`TokenRejected` if it turns the token away.
    """
    try:
        websocket = await open_connection(
            url, token, ssl_context=ssl_context, open_timeout=PROBE_SECONDS
        )
    except HandshakeRefused:
        return False
    if websocket is None:
        return False
    async with websocket:
        return True
