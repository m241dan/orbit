"""When a request held for the user's approval, such as a consultation or a tool call, expires:
an hour after it was held or the last frontend left, whichever is later. Times are seconds on the
caller's clock."""

EXPIRY = 3600.0


def expires_at(held: float, alone_since: float | None) -> float | None:
    """When a request held at ``held`` expires, no frontend having been connected since
    ``alone_since``; none while one is."""
    if alone_since is None:
        return None
    return max(held, alone_since) + EXPIRY
