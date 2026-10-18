"""The token a frontend shows ``orbit serve`` to connect: ``token`` in the config directory,
made on first use and readable by its owner alone."""

import secrets
import stat
from pathlib import Path

from orbit.agent.external.private_files import write_private

TOKEN_FILE = "token"
TOKEN_BYTES = 32


class TokenError(Exception):
    """The token file can't be used; the message names it and says why."""


class TokenFile:
    """The token in ``config_dir``, which grants full control of the control plane."""

    def __init__(self, config_dir: Path) -> None:
        self.path = config_dir / TOKEN_FILE

    def token(self) -> str:
        """The token in the file, made with random bytes first if there's no file.

        Raises :class:`TokenError` if the file can be read by others, is empty, or can't be
        read or written.
        """
        try:
            mode = self.path.stat().st_mode
        except FileNotFoundError:
            return self._made()
        except OSError as exc:
            raise TokenError(f"Can't read the token file {self.path}: {exc}") from None
        if stat.S_IMODE(mode) & 0o077:
            raise TokenError(
                f"The token file {self.path} can be read by others; run chmod 600 {self.path}."
            )
        return self._read()

    def _read(self) -> str:
        """The token the file holds; raises :class:`TokenError` if it holds none."""
        try:
            token = self.path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise TokenError(f"Can't read the token file {self.path}: {exc}") from None
        if not token:
            raise TokenError(f"The token file {self.path} is empty; delete it to make a new one.")
        return token

    def _made(self) -> str:
        """A new token, written to the file; raises :class:`TokenError` if it can't be."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        try:
            write_private(self.path, token + "\n")
        except OSError as exc:
            raise TokenError(f"Can't write the token file {self.path}: {exc}") from None
        return token
