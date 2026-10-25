"""A project's ``.orbit/config.toml`` started from the reference config shipped with orbit."""

import os
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from orbit.agent.external.config_file import CONFIG_PATH
from orbit.agent.external.whole_files import create_whole, write_whole

REFERENCE = files(__package__) / "reference" / "config.toml"
"""The reference config shipped with orbit, as
:func:`~orbit.agent.external.reference_config.reference_config` generates it."""


class ConfigExists(Exception):
    """There's already a project file at ``path``, which only ``force`` replaces."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} already exists.")
        self.path = path


@dataclass(frozen=True)
class InitResult:
    """The reference config written to ``path``, after the file there was kept as ``backup``,
    if there was one."""

    path: Path
    backup: Path | None = None


def init_config(root: Path, force: bool) -> InitResult:
    """Write the reference config to the project file of the workspace at ``root``, making its
    ``.orbit/`` if it's missing. With ``force``, a file already there is first kept as the next
    free ``config.toml.bak.N`` beside it, ``N`` counting from 1, and then replaced.

    Raises :class:`ConfigExists` if there's a file already there and not ``force``, leaving it
    as it was, and ``OSError`` when it can't read the reference or write the file.
    """
    path = root / CONFIG_PATH
    reference = REFERENCE.read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not force:
        try:
            create_whole(path, reference)
        except FileExistsError:
            raise ConfigExists(path) from None
        return InitResult(path)
    backup = _back_up(path)
    write_whole(path, reference)
    return InitResult(path, backup)


def _back_up(path: Path) -> Path | None:
    """The next free ``<name>.bak.N`` beside ``path``, ``N`` counting from 1, made a link to its
    file, so that it's kept once ``path`` is replaced; none if there's no file at ``path``."""
    number = 1
    while True:
        backup = path.with_name(f"{path.name}.bak.{number}")
        try:
            os.link(path, backup)
        except FileExistsError:
            number += 1
        except FileNotFoundError:
            return None
        else:
            return backup
