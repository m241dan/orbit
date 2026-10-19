"""The token a frontend shows ``orbit serve`` to connect: ``token`` in the config directory,
made on first use and readable by its owner alone."""

import secrets
import stat
from pathlib import Path

from orbit.agent.external.private_files import create_private, write_private

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

        Raises :class:`TokenError` if the directory can be written by others, the file can be
        read by others, is empty, or can't be read or written.
        """
        self._check_directory()
        try:
            mode = self.path.stat().st_mode
        except FileNotFoundError:
            return self._created()
        except OSError as exc:
            raise TokenError(f"Can't read the token file {self.path}: {exc}") from None
        if stat.S_IMODE(mode) & 0o077:
            raise TokenError(
                f"The token file {self.path} can be read by others; run chmod 600 {self.path}."
            )
        return self._read()

    def _check_directory(self) -> None:
        """Raises :class:`TokenError` if others could replace the file, by writing its directory,
        or if the directory can't be looked at; one not made yet is made owner-only."""
        directory = self.path.parent
        try:
            mode = directory.stat().st_mode
        except FileNotFoundError:
            return
        except OSError as exc:
            raise TokenError(f"Can't read the token file {self.path}: {exc}") from None
        if stat.S_IMODE(mode) & 0o022:
            raise TokenError(
                f"The token file's directory {directory} can be written by others;"
                f" run chmod 700 {directory}."
            )

    def _read(self) -> str:
        """The token the file holds; raises :class:`TokenError` if it holds none."""
        try:
            token = self.path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise TokenError(f"Can't read the token file {self.path}: {exc}") from None
        if not token:
            raise TokenError(f"The token file {self.path} is empty; delete it to make a new one.")
        return token

    def rotate(self) -> None:
        """Replace the token in the file with a new one, made with random bytes, whole or not at
        all; raises :class:`TokenError` if it can't be written."""
        self._made()

    def _made(self) -> str:
        """A new token, written to the file; raises :class:`TokenError` if it can't be."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        try:
            write_private(self.path, token + "\n")
        except OSError as exc:
            raise TokenError(f"Can't write the token file {self.path}: {exc}") from None
        return token

    def _created(self) -> str:
        """A new token, written to the file unless another was written first, or else the one
        the file holds; raises :class:`TokenError` if neither can be."""
        try:
            create_private(self.path, secrets.token_urlsafe(TOKEN_BYTES) + "\n")
        except FileExistsError:
            pass
        except OSError as exc:
            raise TokenError(f"Can't write the token file {self.path}: {exc}") from None
        return self._read()
