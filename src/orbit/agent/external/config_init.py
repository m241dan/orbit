"""A project's ``.orbit/config.toml`` started from the reference config shipped with orbit, and
brought up to date with it."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from pathlib import Path

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.config_update import Additions, update_text
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


@dataclass(frozen=True)
class UpdateResult:
    """What :func:`update_config` did to the project file at ``path``: added ``additions`` to
    it, or, with none, wrote the reference config there, as there was no file."""

    path: Path
    additions: Additions | None = None


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


def update_config(root: Path, today: Callable[[], date] = date.today) -> UpdateResult:
    """Add to the project file of the workspace at ``root`` what the reference config has that
    it doesn't mention, as :func:`~orbit.agent.domain.config_update.update_text` does on the day
    ``today`` gives, leaving the file as it was if there's nothing to add; without a file, write
    the reference there as :func:`init_config` does.

    Raises :class:`ConfigError`, naming the file, if it isn't UTF-8 or valid TOML, leaving it as
    it was, and ``OSError`` when it can't read the reference or read or write the file.
    """
    path = root / CONFIG_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return UpdateResult(init_config(root, force=False).path)
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    try:
        update = update_text(text, REFERENCE.read_text(encoding="utf-8"), today())
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    if update.text != text:
        write_whole(path, update.text)
    return UpdateResult(path, update.additions)


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
