"""The skills in effect in a workspace: those shipped with orbit in ``skills/``, with the
project's own in ``.orbit/skills/`` over them, a folder for each, read whole, file by file."""

from collections.abc import Callable, Iterator
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import PACKAGE, Layer
from orbit.agent.domain.skills import SkillFile, SkillFolder, SkillLibrary, load_skill_library

SKILL_DIRECTORY = files(__package__) / "skills"
"""The shipped skills' directory in the package: a folder for each skill."""

PROJECT_SKILLS = ".orbit/skills"
"""The project's skills' directory, relative to its root: a folder for each skill."""

type LayerOf = Callable[[str], Layer]
"""The layer of a skill file, given the path it's read from."""


def read_skill_library(root: str, directory: Traversable = SKILL_DIRECTORY) -> SkillLibrary:
    """The skills in effect in the workspace at ``root``: each folder in ``directory``, the
    package's skills, with the files of the project's folder of its name in ``.orbit/skills/``,
    if any, laid over its own, and each of the project's folders of a new name, but hidden
    folders (see :func:`~orbit.agent.domain.skills.load_skill_library`). A skill is of every file
    in its folders at any depth but hidden ones and those in hidden folders, each from the
    package's layer, or from the project's with the file's own path. Files beside the folders are
    left out.

    Raises :class:`ConfigError`, naming the file or folder, if a file isn't UTF-8 text or a skill
    is invalid (see :func:`~orbit.agent.domain.skills.load_skill`).
    """
    project = Path(root) / PROJECT_SKILLS
    package_folders = _read_folders(directory, _packages)
    project_folders = _read_folders(project, _projects) if project.is_dir() else {}
    return load_skill_library(package_folders, project_folders)


def _packages(path: str) -> Layer:
    """The layer of the package's skill file at ``path``: the package's."""
    return PACKAGE


def _projects(path: str) -> Layer:
    """The layer of the project's skill file at ``path``: the project's, with its path."""
    return Layer("project", path)


def skill_names(directory: Traversable = SKILL_DIRECTORY) -> tuple[str, ...]:
    """The names of the skills in ``directory``, the package's skills, a folder each but hidden
    ones, sorted."""
    return tuple(sorted(folder.name for folder in _folders(directory)))


def skill_paths(folder: Traversable, prefix: str = "") -> Iterator[tuple[str, Traversable]]:
    """Each file in the skill's ``folder`` at any depth but hidden ones and those in hidden
    folders, by its path relative to it after ``prefix``."""
    for entry in _visible(folder):
        path = f"{prefix}{entry.name}"
        if entry.is_dir():
            yield from skill_paths(entry, f"{path}/")
        else:
            yield path, entry


def _read_folders(directory: Traversable, layer: LayerOf) -> dict[str, SkillFolder]:
    """Each folder in ``directory`` but hidden ones, by name, its files from the ``layer`` each
    one's path gives.

    Raises :class:`ConfigError`, naming the file, if one isn't UTF-8 text.
    """
    return {folder.name: _read_folder(folder, layer) for folder in _folders(directory)}


def _folders(directory: Traversable) -> Iterator[Traversable]:
    """The folders in ``directory`` but hidden ones."""
    return (folder for folder in _visible(directory) if folder.is_dir())


def _read_folder(folder: Traversable, layer: LayerOf) -> SkillFolder:
    """The skill's ``folder`` as read, its files from the ``layer`` each one's path gives.

    Raises :class:`ConfigError` as :func:`_read_folders` does.
    """
    files = {path: _read_file(entry, layer) for path, entry in skill_paths(folder)}
    return SkillFolder(str(folder), files)


def _visible(folder: Traversable) -> Iterator[Traversable]:
    """What's in ``folder`` but what's hidden, whose name starts with a dot."""
    return (entry for entry in folder.iterdir() if not entry.name.startswith("."))


def _read_file(path: Traversable, layer: LayerOf) -> SkillFile:
    """The skill file at ``path``, its text as written, from the ``layer`` its path gives.

    Raises :class:`ConfigError`, naming the file, if it isn't UTF-8 text.
    """
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: isn't UTF-8 text.") from None
    return SkillFile(text=text, layer=layer(str(path)), location=str(path))
