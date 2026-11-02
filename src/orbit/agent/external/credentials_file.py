"""Credentials from ``credentials.toml`` in orbit's config directory: a flat table of
``NAME = "value"`` strings, readable and writable by its owner alone, read afresh at each
lookup and kept no longer."""

import os
import stat
import tomllib
from pathlib import Path

from orbit.agent.application.ports import CredentialsUnreadable
from orbit.agent.domain.credentials import (
    CREDENTIALS_FILE,
    NonStringCredential,
    credential_values,
    directory_too_open,
    invalid_toml,
    not_a_string,
    too_open,
    unreadable,
)

_OTHERS_IN = 0o066  # Readable or writable by the group or others.
_OTHERS_WRITE = 0o022  # Writable by the group or others.


class CredentialsFile:
    """The credentials in ``credentials.toml`` in ``config_dir``; none if there's no file."""

    def __init__(self, config_dir: Path) -> None:
        self.path = config_dir / CREDENTIALS_FILE

    def __call__(self, name: str) -> str | None:
        """The value the file gives the variable ``name``; ``None`` if it gives none or there's
        no file. Raises :class:`CredentialsUnreadable` if the file or its directory lets others
        in, or the file can't be read, isn't valid TOML or holds a value that isn't a string."""
        text = self._text()
        if text is None:
            return None
        try:
            table = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise CredentialsUnreadable(invalid_toml(str(self.path), str(exc))) from None
        values = credential_values(table)
        if isinstance(values, NonStringCredential):
            raise CredentialsUnreadable(not_a_string(str(self.path), values))
        return values.get(name)

    def _text(self) -> str | None:
        """What the file holds, checked private before it's read; ``None`` if there's none."""
        data = self._bytes()
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise CredentialsUnreadable(unreadable(str(self.path), "it isn't UTF-8")) from None

    def _bytes(self) -> bytes | None:
        """The file's bytes, checked private before they're read; ``None`` if there's no file,
        or no directory to hold it."""
        try:
            with self.path.open("rb") as file:
                self._check_private(os.fstat(file.fileno()).st_mode)
                return file.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CredentialsUnreadable(unreadable(str(self.path), exc.strerror or "")) from None

    def _check_private(self, file_mode: int) -> None:
        """Raise :class:`CredentialsUnreadable` if others could replace the open file, by
        writing to its directory, or could read or write it, its mode being ``file_mode``."""
        directory = self.path.parent
        if stat.S_IMODE(directory.stat().st_mode) & _OTHERS_WRITE:
            raise CredentialsUnreadable(directory_too_open(str(directory)))
        if stat.S_IMODE(file_mode) & _OTHERS_IN:
            raise CredentialsUnreadable(too_open(str(self.path)))
