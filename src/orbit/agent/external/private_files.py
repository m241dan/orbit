"""Files readable by their owner alone, each written whole or not at all."""

import os
import tempfile
from pathlib import Path


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, readable and writable by its owner alone, through a temporary
    file beside it that replaces ``path`` only once it's whole; the directory is made owner-only
    if it has to be made. Raises ``OSError`` when it can't."""
    temporary = _whole_temporary(path, text)
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def create_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as :func:`write_private` does, but only if there's no file
    there yet, so that the first of several writers wins. Raises ``FileExistsError`` if there
    is one, and ``OSError`` when it can't."""
    temporary = _whole_temporary(path, text)
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink()


def _whole_temporary(path: Path, text: str) -> Path:
    """A temporary file beside ``path`` holding ``text``, readable and writable by its owner
    alone; the directory is made owner-only if it has to be made."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with open(descriptor, "w", encoding="utf-8") as file:
            os.fchmod(descriptor, 0o600)
            file.write(text)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary
