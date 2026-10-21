"""The WebSocket transport's numbers, which the control plane and its frontends share, as
described in ``docs/protocol.md``."""

import ipaddress
from collections.abc import Mapping

NORMAL = 1000
"""The close code of a WebSocket whose stream ended, as an incompatible ``Hello``'s does."""

GOING_AWAY = 1001
"""The close code of every WebSocket as the control plane shuts down."""

POLICY_VIOLATION = 1008
"""The close code of a WebSocket that sent a frame with no readable ``command_id``, or fell
too far behind."""

MESSAGE_TOO_BIG = 1009
"""The close code of a WebSocket a message over the limit was sent to, or from."""

SHUTTING_DOWN = "orbit serve is shutting down"
"""The close reason of every WebSocket, and the body of every upgrade turned away, as the
control plane shuts down."""

UNAUTHORIZED = 401
"""The HTTP status of an upgrade turned away for its token."""

FORBIDDEN = 403
"""The HTTP status of an upgrade turned away, under mutual TLS, for a revoked client
certificate."""

UNAVAILABLE = 503
"""The HTTP status of an upgrade turned away as the control plane shuts down."""

CERTIFICATE_CHALLENGE = "ClientCertificate"
"""The ``WWW-Authenticate`` challenge of an HTTP 401 turning away, under mutual TLS, a client
that showed no certificate, which tells it from a token turned away."""

DEFAULT_MAX_MESSAGE_MB = 64
"""The largest message either way, in MiB, unless the server settings say otherwise."""

LOCALHOST = "localhost"
"""The hostname of this machine's loopback interface."""

KEY_READABLE = (
    "The private key can be read by others. {path} must be readable by you alone: run"
    " chmod 600 {path}."
)
"""A private key at ``path`` others can read, which neither side will use."""


def bearer_header(token: str) -> dict[str, str]:
    """The upgrade's header showing ``token``."""
    return {"Authorization": f"Bearer {token}"}


def bearer_token(headers: Mapping[str, str] | None) -> str:
    """The token in a ``Bearer`` authorization header; empty when there isn't one."""
    scheme, _, token = (headers or {}).get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def is_loopback(host: str) -> bool:
    """Whether ``host`` is ``localhost`` or a loopback address, reachable from this machine
    alone."""
    if host.lower() == LOCALHOST:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def too_open(mode: int) -> bool:
    """Whether a file of ``mode`` lets its group or others in, as a private key or token file
    mustn't."""
    return bool(mode & 0o077)
