"""When a request held for the user's approval, such as a consultation or a tool call, expires:
an hour after it was held or the last frontend left, whichever is later. Times are lengths of
time since the caller's clock's epoch."""

from datetime import timedelta

EXPIRY = timedelta(hours=1)
"""How long a request is held with no frontend connected before it expires."""


def expires_at(held: timedelta, alone_since: timedelta | None) -> timedelta | None:
    """When a request held at ``held`` expires, no frontend having been connected since
    ``alone_since``; none while one is."""
    if alone_since is None:
        return None
    return max(held, alone_since) + EXPIRY
