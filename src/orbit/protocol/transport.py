"""The WebSocket transport's numbers, which the control plane and its frontends share, as
described in ``docs/protocol.md``."""

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

UNAVAILABLE = 503
"""The HTTP status of an upgrade turned away as the control plane shuts down."""

CERTIFICATE_CHALLENGE = "ClientCertificate"
"""The ``WWW-Authenticate`` challenge of an HTTP 401 turning away, under mutual TLS, a client
that showed no certificate, which tells it from a token turned away."""

DEFAULT_MAX_MESSAGE_MB = 64
"""The largest message either way, in MiB, unless the server settings say otherwise."""


def bearer_header(token: str) -> dict[str, str]:
    """The upgrade's header showing ``token``."""
    return {"Authorization": f"Bearer {token}"}
