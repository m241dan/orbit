"""The token a frontend shows the control plane, read from the file ``orbit serve`` made."""

from pathlib import Path


class TokenUnreadable(Exception):
    """The token file can't be used; the message names it and says why."""


def read_token(path: Path) -> str:
    """The token in the file at ``path``.

    Raises :class:`TokenUnreadable` if it can't be read or holds none.
    """
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise TokenUnreadable(f"Can't read the token file {path}: {exc}") from None
    if not token:
        raise TokenUnreadable(f"The token file {path} is empty.")
    return token
