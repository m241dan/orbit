"""A project's ``.orbit/config.toml`` started from the reference config shipped with orbit,
brought up to date with it, and given copies of the built-in roles' instructions to edit; the
user's own config started from the user reference config where there's none, or brought up to
date with it; and the project given copies of orbit's skills to edit."""

import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal, assert_never

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.config_update import (
    Additions,
    InstructionLines,
    add_instructions,
    instructions_copy,
    update_text,
)
from orbit.agent.external.built_in_roles import ROLE_DIRECTORY, read_instruction_files
from orbit.agent.external.config_file import CONFIG_PATH, REFERENCE
from orbit.agent.external.reference_config import LATER_HEADING
from orbit.agent.external.skill_files import (
    PROJECT_SKILLS,
    SKILL_DIRECTORY,
    skill_names,
    skill_paths,
)
from orbit.agent.external.user_config import USER_CONFIG_FILE, USER_REFERENCE
from orbit.agent.external.whole_files import create_whole, write_whole


class ConfigExists(Exception):
    """There's already a config file at ``path``, which only ``"replace"`` replaces."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} already exists.")
        self.path = path


class UnknownSkill(Exception):
    """``name`` names none of orbit's ``skills``."""

    def __init__(self, name: str, skills: tuple[str, ...]) -> None:
        super().__init__(f"No skill named {name}; orbit's skills are {', '.join(skills)}.")
        self.name = name
        self.skills = skills


type ExistingFile = Literal["keep", "replace"]
"""What writing a config does with a file already there: keeps it, refusing to write, or
replaces it after keeping it as a numbered backup."""

type FileCopy = Literal["copied", "kept"]
"""What copying a file did: copied it, or kept the file already there."""

type SkillFiles = Literal["some copied", "none copied"]
"""Whether copying a skill copied any of its files."""

type ConfigFile = Literal["written", "existing"]
"""Whether the project file was written with the reference config, as there was none, or was
there already."""


@dataclass(frozen=True)
class InitResult:
    """The reference config written to ``path``, after the file there was kept as ``backup``,
    if there was one."""

    path: Path
    backup: Path | None = None


@dataclass(frozen=True)
class UpdateResult:
    """What :func:`update_config` or :func:`update_user_config` did to the file at ``path``:
    added ``additions`` to it, or, with none, wrote the reference config there, as there was no
    file."""

    path: Path
    additions: Additions | None = None


@dataclass(frozen=True)
class RolesResult:
    """What :func:`add_roles` did: the built-in roles' instructions it ``copied`` to the files
    there, and the files it ``kept``, being there already, by role; and the ``lines`` it added to
    the project file at ``path``, after writing the reference config there if ``config`` says it
    was ``"written"``, as there was no file."""

    path: Path
    copied: dict[str, Path]
    kept: dict[str, Path]
    lines: InstructionLines
    config: ConfigFile = "existing"


@dataclass(frozen=True)
class SkillCopy:
    """What :func:`add_skills` did with one of orbit's skills: copied its files to ``folder``,
    if ``copied`` says some were, and ``kept`` the files already there, in path order."""

    folder: Path
    copied: SkillFiles
    kept: tuple[Path, ...]


@dataclass(frozen=True)
class _ConfigKind:
    """A kind of config file: started from ``reference``, its directory made with
    ``directory_mode`` if it's missing, and updated with a later version's settings from
    ``later_heading`` on, if there's one."""

    reference: Traversable
    directory_mode: int
    later_heading: str | None


_PROJECT = _ConfigKind(REFERENCE, 0o777, LATER_HEADING)
"""A project's ``.orbit/config.toml``, its ``.orbit/`` made as the umask allows."""

_USER = _ConfigKind(USER_REFERENCE, 0o700, None)
"""The user's own ``config.toml``, its directory readable by its owner alone."""


def init_config(root: Path, existing: ExistingFile) -> InitResult:
    """Write the reference config to the project file of the workspace at ``root``, making its
    ``.orbit/`` if it's missing. With ``existing`` ``"replace"``, a file already there is first
    kept as the next free ``config.toml.bak.N`` beside it, ``N`` counting from 1, and then
    replaced.

    Raises :class:`ConfigExists` if there's a file already there and ``existing`` is
    ``"keep"``, leaving it as it was, and ``OSError`` when it can't read the reference or write
    the file.
    """
    return _init(root / CONFIG_PATH, _PROJECT, existing)


def init_user_config(config_dir: Path, existing: ExistingFile) -> InitResult:
    """Write the user reference config to the user config in ``config_dir``, making
    ``config_dir`` and any missing parents, with only ``config_dir`` readable by its owner
    alone; directories already there keep their modes. With ``existing`` ``"replace"``, a file
    already there is first kept as the next free ``config.toml.bak.N`` beside it, ``N``
    counting from 1, and then replaced.

    Raises :class:`ConfigExists` if there's a file already there and ``existing`` is
    ``"keep"``, leaving it as it was, and ``OSError`` when it can't read the reference or write
    the file.
    """
    return _init(config_dir / USER_CONFIG_FILE, _USER, existing)


def create_user_config(config_dir: Path) -> Path | None:
    """The user config in ``config_dir``, written with the user reference config as
    :func:`init_user_config` does if there's no file there; ``None`` if there is one, even one
    another writer made first, which is left as it was.

    Raises ``OSError`` when it can't read the reference or write the file.
    """
    try:
        return init_user_config(config_dir, "keep").path
    except ConfigExists:
        return None


def _init(path: Path, kind: _ConfigKind, existing: ExistingFile) -> InitResult:
    """Write ``kind``'s reference to ``path``, making its directory, and missing parents, if
    it's missing, as :func:`_write_reference` does with ``existing``.

    Raises :class:`ConfigExists` if there's a file already there and ``existing`` is
    ``"keep"``, leaving it as it was, and ``OSError`` when it can't read the reference or write
    the file.
    """
    reference = kind.reference.read_text(encoding="utf-8")
    path.parent.mkdir(mode=kind.directory_mode, parents=True, exist_ok=True)
    return _write_reference(path, reference, existing)


def _write_reference(path: Path, reference: str, existing: ExistingFile) -> InitResult:
    """Write ``reference`` to ``path``, its directory being there; with ``existing``
    ``"replace"``, after keeping a file already there as :func:`_back_up` does.

    Raises :class:`ConfigExists` if there's a file already there and ``existing`` is
    ``"keep"``, leaving it as it was, and ``OSError`` when it can't write the file.
    """
    match existing:
        case "keep":
            return _create_reference(path, reference)
        case "replace":
            backup = _back_up(path)
            write_whole(path, reference)
            return InitResult(path, backup)
        case _:
            assert_never(existing)


def _create_reference(path: Path, reference: str) -> InitResult:
    """Write ``reference`` to ``path``, where there's no file.

    Raises :class:`ConfigExists` if there's a file already there, leaving it as it was, and
    ``OSError`` when it can't write the file.
    """
    # create_whole makes its temporary file first, so a read-only directory would hide an
    # existing file behind a PermissionError.
    if os.path.lexists(path):
        raise ConfigExists(path)
    try:
        create_whole(path, reference)
    except FileExistsError:
        raise ConfigExists(path) from None
    return InitResult(path)


def update_config(root: Path, today: Callable[[], date] = date.today) -> UpdateResult:
    """Add to the project file of the workspace at ``root`` what the reference config has that
    it doesn't mention, as :func:`~orbit.agent.domain.config_update.update_text` does on the day
    ``today`` gives, its settings from :data:`LATER_HEADING` on being a later version's, leaving
    the file as it was if there's nothing to add; without a file, write the reference there as
    :func:`init_config` does.

    Raises :class:`ConfigError`, naming the file, if it isn't UTF-8 or valid TOML, leaving it as
    it was, and ``OSError`` when it can't read the reference or read or write the file.
    """
    return _update(root / CONFIG_PATH, _PROJECT, today)


def update_user_config(config_dir: Path, today: Callable[[], date] = date.today) -> UpdateResult:
    """Add to the user config in ``config_dir`` what the user reference config has that it
    doesn't mention, as :func:`~orbit.agent.domain.config_update.update_text` does on the day
    ``today`` gives, leaving the file as it was if there's nothing to add; without a file, write
    the user reference there as :func:`init_user_config` does.

    Raises :class:`ConfigError`, naming the file, if it isn't UTF-8 or valid TOML, leaving it as
    it was, and ``OSError`` when it can't read the reference or read or write the file.
    """
    return _update(config_dir / USER_CONFIG_FILE, _USER, today)


def _update(path: Path, kind: _ConfigKind, today: Callable[[], date]) -> UpdateResult:
    """Add to the config file at ``path`` what ``kind``'s reference has that it doesn't
    mention, as :func:`~orbit.agent.domain.config_update.update_text` does on the day ``today``
    gives, leaving the file as it was if there's nothing to add; without a file, write the
    reference there as :func:`_init` does.

    Raises :class:`ConfigError`, naming the file, if it isn't UTF-8 or valid TOML, leaving it as
    it was, and ``OSError`` when it can't read the reference or read or write the file.
    """
    text = _read_config_text(path)
    if text is None:
        return UpdateResult(_init(path, kind, "keep").path)
    with _naming(path):
        reference = kind.reference.read_text(encoding="utf-8")
        update = update_text(text, reference, today(), later_heading=kind.later_heading)
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
    text = _read_config_text(path)
    config: ConfigFile = "written" if text is None else "existing"
    if text is None:
        init_config(root, "keep")
    current = reference if text is None else text
    built_in = read_instruction_files()
    with _naming(path):
        update = add_instructions(
            current, reference, tuple(built_in), today(), later_heading=LATER_HEADING
        )
    copied, kept = _copy_instructions(path.parent, built_in)
    if update.text != current:
        write_whole(path, update.text)
    return RolesResult(path, copied, kept, update.lines, config)


def add_skills(
    root: Path, names: Iterable[str], directory: Traversable = SKILL_DIRECTORY
) -> dict[str, SkillCopy]:
    """Copy each file of each of orbit's skills ``names`` names, the folders in ``directory``,
    at any depth but hidden ones, to its path under the skill's folder in the ``.orbit/skills/``
    of the workspace at ``root``, making folders as needed and keeping a file already there;
    what was done with each skill, by name, in the order named, a skill named twice once.

    Raises :class:`UnknownSkill` for the first name that isn't one of orbit's skills, copying
    nothing, :class:`ConfigError`, naming the folder, when one of orbit's can't be listed, and
    ``OSError`` when it can't read orbit's files or write the workspace's.
    """
    named = tuple(dict.fromkeys(names))
    known = skill_names(directory)
    for name in named:
        if name not in known:
            raise UnknownSkill(name, known)
    return {name: _copy_skill(directory / name, root / PROJECT_SKILLS / name) for name in named}


def _read_config_text(path: Path) -> str | None:
    """The text of the config file at ``path``; none if there's no file.

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
    targets = {name: directory / instructions_copy(name) for name in built_in}
    outcomes = {
        name: _copy_file(targets[name], (ROLE_DIRECTORY / file).read_text(encoding="utf-8"))
        for name, file in built_in.items()
    }
    return (
        {name: targets[name] for name in _having(outcomes, "copied")},
        {name: targets[name] for name in _having(outcomes, "kept")},
    )


def _copy_skill(source: Traversable, folder: Path) -> SkillCopy:
    """Copy each file of the skill in ``source``, its text as written, to its path under
    ``folder``, in path order, keeping a file already there; what was done.

    Raises :class:`ConfigError`, naming the folder, when one can't be listed, and ``OSError``
    when it can't read a file or write its copy.
    """
    files = dict(skill_paths(source))
    outcomes = {
        folder / path: _copy_file(folder / path, files[path].read_bytes().decode("utf-8"))
        for path in sorted(files)
    }
    copied: SkillFiles = "some copied" if "copied" in outcomes.values() else "none copied"
    return SkillCopy(folder, copied, _having(outcomes, "kept"))


def _having[K](outcomes: Mapping[K, FileCopy], outcome: FileCopy) -> tuple[K, ...]:
    """The keys of ``outcomes`` whose copy had ``outcome``, in order."""
    return tuple(key for key, found in outcomes.items() if found == outcome)


def _copy_file(target: Path, text: str) -> FileCopy:
    """Write ``text`` to ``target``, making its folders as needed, unless there's a file there
    already, which is kept; which was done.

    Raises ``OSError`` when it can't write the file.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        create_whole(target, text)
    except FileExistsError:
        return "kept"
    return "copied"


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
