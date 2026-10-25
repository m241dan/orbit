"""A project's ``.orbit/config.toml`` started from the reference config shipped with orbit,
brought up to date with it, and given copies of the built-in roles' instructions to edit."""

import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from pathlib import Path

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.config_update import (
    Additions,
    InstructionLines,
    add_instructions,
    instructions_copy,
    update_text,
)
from orbit.agent.external.built_in_roles import ROLE_DIRECTORY, read_instruction_files
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


@dataclass(frozen=True)
class RolesResult:
    """What :func:`add_roles` did: the built-in roles' instructions it ``copied`` to the files
    there, and the files it ``kept``, being there already, by role; and the ``lines`` it added to
    the project file at ``path``, after writing the reference config there if it ``wrote`` it,
    as there was no file."""

    path: Path
    copied: dict[str, Path]
    kept: dict[str, Path]
    lines: InstructionLines
    wrote: bool = False


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
    text = _read_project(path)
    if text is None:
        return UpdateResult(init_config(root, force=False).path)
    with _naming(path):
        update = update_text(text, REFERENCE.read_text(encoding="utf-8"), today())
    if update.text != text:
        write_whole(path, update.text)
    return UpdateResult(path, update.additions)


def add_roles(root: Path, today: Callable[[], date] = date.today) -> RolesResult:
    """Copy each built-in role's instructions beside the project file of the workspace at
    ``root``, where :func:`~orbit.agent.domain.config_update.instructions_copy` puts them,
    keeping a file already there, and add to the project file the commented-out lines pointing
    the roles at their copies, as :func:`~orbit.agent.domain.config_update.add_instructions`
    does on the day ``today`` gives; without a file, first write the reference config there as
    :func:`init_config` does.

    Raises :class:`ConfigError`, naming the file, if it isn't UTF-8 or valid TOML, leaving it as
    it was and copying nothing, and ``OSError`` when it can't read orbit's files or read or
    write the workspace's.
    """
    path = root / CONFIG_PATH
    reference = REFERENCE.read_text(encoding="utf-8")
    text = _read_project(path)
    wrote = text is None
    if text is None:
        text = reference
        init_config(root, force=False)
    built_in = read_instruction_files()
    with _naming(path):
        update = add_instructions(text, reference, tuple(built_in), today())
    copied, kept = _copy_instructions(path.parent, built_in)
    if update.text != text:
        write_whole(path, update.text)
    return RolesResult(path, copied, kept, update.lines, wrote)


def _read_project(path: Path) -> str | None:
    """The text of the project file at ``path``; none if there's no file.

    Raises :class:`ConfigError`, naming the file, if it isn't UTF-8, and ``OSError`` when it
    can't read it.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None


@contextmanager
def _naming(path: Path) -> Iterator[None]:
    """Run the block, raising each :class:`ConfigError` it raises again naming ``path``."""
    try:
        yield
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def _copy_instructions(
    directory: Path, built_in: Mapping[str, str]
) -> tuple[dict[str, Path], dict[str, Path]]:
    """Copy each role's instructions from the Markdown file ``built_in`` names for it to its
    file under ``directory``, keeping a file already there; the files copied to, and those kept,
    by role.

    Raises ``OSError`` when it can't read a role's file or write its copy.
    """
    copied: dict[str, Path] = {}
    kept: dict[str, Path] = {}
    for name, file in built_in.items():
        target = directory / instructions_copy(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            create_whole(target, (ROLE_DIRECTORY / file).read_text(encoding="utf-8"))
        except FileExistsError:
            kept[name] = target
        else:
            copied[name] = target
    return copied, kept


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
