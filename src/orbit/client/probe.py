"""Whether a control plane takes a frontend's token, asked with an upgrade that's closed at
once."""

from orbit.client.upgrade import HandshakeRefused, open_connection

PROBE_SECONDS = 5
"""How long the upgrade may take before the control plane is taken as unreachable."""


async def accepts(url: str, token: str) -> bool:
    """Whether the control plane at ``url`` takes a connection showing ``token``; ``False`` if
    it can't be reached, safely or at all.

    Raises :class:`TokenRejected` if it turns the token away.
    """
    try:
        websocket = await open_connection(url, token, open_timeout=PROBE_SECONDS)
    except HandshakeRefused:
        return False
    if websocket is None:
        return False
    async with websocket:
        return True
