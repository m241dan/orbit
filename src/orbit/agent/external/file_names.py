"""The files in a directory, for moving a requirements record whose number was taken."""

import os


def file_names(path: str) -> tuple[str, ...]:
    """The names of the files in the directory at ``path``; none if it can't be read, such as
    when it doesn't exist."""
    try:
        with os.scandir(path) as entries:
            return tuple(entry.name for entry in entries if entry.is_file())
    except OSError:
        return ()
