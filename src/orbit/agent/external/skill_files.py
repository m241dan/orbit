"""The skills in effect in a workspace: for now, those shipped with orbit in ``skills/``, a folder
for each, read whole, file by file."""

from collections.abc import Iterator
from importlib.resources import files
from importlib.resources.abc import Traversable

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import PACKAGE
from orbit.agent.domain.skills import Skill, SkillFile, SkillLibrary, load_skill

SKILL_DIRECTORY = files(__package__) / "skills"
"""The shipped skills' directory in the package: a folder for each skill."""


def read_skill_library(root: str, directory: Traversable = SKILL_DIRECTORY) -> SkillLibrary:
    """The skills in effect in the workspace at ``root``: each folder in ``directory``, the
    package's skills, but hidden ones, a skill of every file in it at any depth but hidden ones
    and those in hidden folders, each from the package's layer. Files beside the folders are
    left out. The workspace's own skills aren't read yet.

    Raises :class:`ConfigError`, naming the file or folder, if a file isn't UTF-8 text or a skill
    is invalid (see :func:`~orbit.agent.domain.skills.load_skill`).
    """
    skills = [_read_skill(folder) for folder in _visible(directory) if folder.is_dir()]
    return SkillLibrary(skills={skill.name: skill for skill in skills})


def _read_skill(folder: Traversable) -> Skill:
    """The skill of the files in ``folder``.

    Raises :class:`ConfigError` as :func:`read_skill_library` does.
    """
    return load_skill(folder.name, str(folder), dict(_read_files(folder)))


def _read_files(folder: Traversable, prefix: str = "") -> Iterator[tuple[str, SkillFile]]:
    """Each file in ``folder`` at any depth but hidden ones and those in hidden folders, by its
    path relative to it after ``prefix``.

    Raises :class:`ConfigError`, naming the file, if one isn't UTF-8 text.
    """
    for entry in _visible(folder):
        path = f"{prefix}{entry.name}"
        if entry.is_dir():
            yield from _read_files(entry, f"{path}/")
        else:
            yield path, _read_file(entry)


def _visible(folder: Traversable) -> Iterator[Traversable]:
    """What's in ``folder`` but what's hidden, whose name starts with a dot."""
    return (entry for entry in folder.iterdir() if not entry.name.startswith("."))


def _read_file(path: Traversable) -> SkillFile:
    """The package's file at ``path``, its text as written.

    Raises :class:`ConfigError`, naming the file, if it isn't UTF-8 text.
    """
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: isn't UTF-8 text.") from None
    return SkillFile(text=text, layer=PACKAGE, location=str(path))
