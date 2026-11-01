"""Skills: folders of Markdown, and any other files they need, that teach agents how the user
builds software. Each opens with a ``SKILL.md`` whose front matter names and describes it, held
to the rules Claude Code holds skills to. They come from two layers, the package's skills and the
project's, whose files replace and add to the package's file by file. Roles name the skills
their agents get, and a project also maps paths in it to the skills for the code there; an agent's
instructions deliver the skills it's given, and it reads their files, and only theirs."""

import posixpath
import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import takewhile

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import Layer

SKILL_FILE = "SKILL.md"
"""The file in a skill's folder that opens it: its front matter, then its text."""

FRONT_MATTER_LINE = "---"
"""The line above and below a ``SKILL.md``'s front matter."""

NEEDS_FRONT_MATTER = "needs front matter between --- lines, with name and description."
"""Why a ``SKILL.md`` without front matter naming and describing its skill is refused."""

SKILL_NAME = re.compile(r"[a-z0-9-]{1,64}")
"""The names Claude Code takes for a skill."""

MAX_DESCRIPTION = 1024
"""The most characters Claude Code takes in a skill's description."""

NO_SKILLS = "You weren't given any skills."
"""Why an agent given no skills can't read one."""

SKILLS_HEADING = "## Skills"
"""The heading over the skills an agent's instructions deliver."""

READING_LINE = (
    'A line such as "Read `code-style`\'s `testing.md`." means: call read_skill with skill'
    " `code-style` and file `testing.md`."
)
"""How an agent reads a file of a skill that its skills point it to."""

OUTSIDE_PROJECT = "paths are relative to the project root and stay inside it."
"""Why a ``[[skills]]`` entry's path that's empty, holds a NUL byte, is absolute or leaves the
project is refused."""

_BYTE_ORDER_MARK = "\ufeff"
"""What some editors write before a UTF-8 file's text."""

_BLOCK_SCALAR = re.compile(r"[>|][-+]?")
"""A value that opens a YAML block scalar, its text the indented lines below its key."""

_BLOCK_JOINERS = {">": " ", "|": "\n"}
"""What joins a block scalar's lines, by its first character: ``>`` folds them, ``|`` keeps them."""

_QUOTED = re.compile(r"\"(?P<double>.*)\"|'(?P<single>.*)'")
"""A value in double or single quotes."""

_ESCAPED = re.compile(r'\\(["\\])')
"""A backslash and the quote or backslash it escapes in a double-quoted value."""


class _Skills(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SkillFile(_Skills):
    """A file of a skill: its ``text``, the ``layer`` it came from, and its ``location``, the
    path errors name it by."""

    text: str
    layer: Layer
    location: str


class Skill(_Skills):
    """A skill, by its front matter's ``name`` and ``description``, with its ``SKILL.md``'s
    ``body``, the text after the front matter, and its ``files``, every one by its path relative
    to the skill's folder, ``SKILL.md`` included, in order of their paths."""

    name: str
    description: str
    body: str
    files: dict[str, SkillFile]


class SkillLibrary(_Skills):
    """The ``skills`` agents can be given, by name, in name order."""

    skills: dict[str, Skill] = {}

    @field_validator("skills")
    @classmethod
    def _in_name_order(cls, skills: dict[str, Skill]) -> dict[str, Skill]:
        return dict(sorted(skills.items()))


class SkillMapping(_Skills):
    """Skills for the code under some paths: the ``paths``, files or folders in the project,
    relative to its root, and the skills that apply there, ``use``, by name."""

    paths: tuple[str, ...] = Field(
        min_length=1,
        description="Files or folders in the project, relative to its root; each must exist.",
    )
    use: tuple[str, ...] = Field(min_length=1, description="The skills that apply there, by name.")


@dataclass(frozen=True)
class SkillFolder:
    """A skill's folder in one layer as read: its ``location``, the path errors name it by, and
    its ``files``, by their paths relative to it."""

    location: str
    files: Mapping[str, SkillFile]


@dataclass(frozen=True)
class FrontMatter:
    """What a ``SKILL.md``'s front matter gives: its skill's ``name`` and ``description``, and the
    ``body``, the text below it."""

    name: str
    description: str
    body: str


def parse_front_matter(text: str) -> FrontMatter:
    """The front matter ``text``, a ``SKILL.md``'s, opens with, after any byte-order mark: a first
    line ``---``, then ``key: value`` lines up to the next ``---``, of which ``name`` and
    ``description`` are kept, read as Claude Code's YAML reads them (see :func:`_keys`); the body
    is the text below, from its first line that isn't blank. Lines that aren't keys, and indented
    ones, which belong to the key above, are ignored.

    Raises :class:`ConfigError` saying so if there's no front matter or it lacks a name or a
    description.
    """
    lines = text.removeprefix(_BYTE_ORDER_MARK).splitlines(keepends=True)
    end = _closing_line(lines)
    keys = _keys(lines[1:end])
    if "name" not in keys or "description" not in keys:
        raise ConfigError(NEEDS_FRONT_MATTER)
    body = "".join(lines[end + 1 :]).lstrip("\r\n")
    return FrontMatter(keys["name"], keys["description"], body)


def _closing_line(lines: list[str]) -> int:
    """The index of the line that closes the front matter ``lines`` open with.

    Raises :class:`ConfigError` if they don't open with front matter.
    """
    if not lines or lines[0].rstrip() != FRONT_MATTER_LINE:
        raise ConfigError(NEEDS_FRONT_MATTER)
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip() == FRONT_MATTER_LINE:
            return index
    raise ConfigError(NEEDS_FRONT_MATTER)


def _keys(lines: list[str]) -> dict[str, str]:
    """The value of each key the front matter's ``lines`` give, by key: each unindented
    ``key: value`` line's, with the spaces around both left out, then read as :func:`_value`
    reads it."""
    keys: dict[str, str] = {}
    for index, line in enumerate(lines):
        key, colon, value = line.partition(":")
        if colon and not line[:1].isspace():
            keys[key.strip()] = _value(value.strip(), lines[index + 1 :])
    return keys


def _value(written: str, below: list[str]) -> str:
    """The value ``written`` after a key gives, the front matter's lines ``below`` that key
    following: a block scalar's text if it opens one (see :func:`_block_scalar`), and otherwise
    what it says without its quotes (see :func:`_unquoted`)."""
    if _BLOCK_SCALAR.fullmatch(written):
        return _block_scalar(_BLOCK_JOINERS[written[0]], below)
    return _unquoted(written)


def _block_scalar(joiner: str, below: list[str]) -> str:
    """The text of a block scalar the lines ``below`` its key hold: the indented lines at their
    top, each without its indentation, joined by ``joiner`` (a space where ``>`` folds them, a
    newline where ``|`` keeps them), with the spaces around the whole left out."""
    block = takewhile(lambda line: line[:1].isspace(), below)
    return joiner.join(line.strip() for line in block).strip()


def _unquoted(written: str) -> str:
    """The value ``written``, without one pair of matching quotes around it if it has them:
    inside single quotes ``''`` stands for ``'``, and inside double quotes ``\\"`` for ``"`` and
    ``\\\\`` for ``\\``."""
    quoted = _QUOTED.fullmatch(written)
    if quoted is None:
        return written
    if quoted["single"] is not None:
        return quoted["single"].replace("''", "'")
    return _ESCAPED.sub(r"\1", quoted["double"])


def load_skill(folder: str, location: str, files: Mapping[str, SkillFile]) -> Skill:
    """The skill in the folder named ``folder``, at ``location``, of its ``files``, by their
    paths relative to it, its name and description given by its ``SKILL.md``'s front matter.

    Raises :class:`ConfigError` naming the folder if it has no ``SKILL.md``, and naming the
    ``SKILL.md`` if it has no front matter (see :func:`parse_front_matter`), its name isn't the
    folder's or isn't one Claude Code takes, or its description is empty or too long for Claude
    Code.
    """
    skill_md = files.get(SKILL_FILE)
    if skill_md is None:
        raise ConfigError(f"{location}: a skill needs a {SKILL_FILE}.")
    try:
        front = parse_front_matter(skill_md.text)
    except ConfigError as exc:
        raise ConfigError(f"{skill_md.location}: {exc}") from None
    problem = _front_matter_problem(front, folder)
    if problem is not None:
        raise ConfigError(f"{skill_md.location}: {problem}")
    return Skill(
        name=front.name,
        description=front.description,
        body=front.body,
        files=dict(sorted(files.items())),
    )


def _front_matter_problem(front: FrontMatter, folder: str) -> str | None:
    """Why the ``front`` matter of the ``SKILL.md`` in the folder named ``folder`` is refused, if
    it is: its name isn't the folder's or isn't one Claude Code takes, or its description is empty
    or too long."""
    if front.name != folder:
        return f"its name is {front.name} but its folder is {folder}."
    if not SKILL_NAME.fullmatch(front.name):
        return "a skill's name is at most 64 lowercase letters, digits and hyphens."
    if not 1 <= len(front.description) <= MAX_DESCRIPTION:
        return "a skill's description is 1 to 1,024 characters."
    return None


def load_skill_library(
    package: Mapping[str, SkillFolder], project: Mapping[str, SkillFolder]
) -> SkillLibrary:
    """The skills of the ``package``'s folders and the ``project``'s, each by its folder's name:
    a skill's files are the package's with the project's laid over them, a project file
    replacing the package's at its path whole and one at a new path added, and a project folder
    whose name the package has no folder of is a skill of its own.

    Raises :class:`ConfigError` as :func:`load_skill` does, naming a skill's folder by the
    package's location if it has one there.
    """
    folders = dict(package)
    for name, folder in project.items():
        shipped = package.get(name)
        folders[name] = folder if shipped is None else _laid_over(shipped, folder)
    skills = (load_skill(name, folder.location, folder.files) for name, folder in folders.items())
    return SkillLibrary(skills={skill.name: skill for skill in skills})


def _laid_over(shipped: SkillFolder, folder: SkillFolder) -> SkillFolder:
    """The ``shipped`` folder with the files of the project's ``folder`` laid over its own, each
    replacing the one at its path, if any."""
    return SkillFolder(shipped.location, {**shipped.files, **folder.files})


def inside_project(path: str) -> bool:
    """Whether ``path`` isn't empty, holds no NUL byte, which no path on disk can, is relative
    and, its ``..`` worked out, stays inside the project."""
    normal = posixpath.normpath(path)
    outside = posixpath.isabs(normal) or normal == ".." or normal.startswith("../")
    return path != "" and "\x00" not in path and not outside


def unknown_skill(key: str, name: str, library: SkillLibrary) -> str:
    """Why the skill ``name`` the setting ``key`` names is refused: it isn't one of the
    ``library``'s, which it lists."""
    return f"{key} names {name}, which isn't a skill; the skills are {', '.join(library.skills)}."


def given_skills(
    role_skills: Sequence[str], task_skills: Sequence[str], mappings: Sequence[SkillMapping]
) -> tuple[str, ...]:
    """The skills an agent is given, by name: its role's ``role_skills``, then its task's
    ``task_skills``, or, for a task without any, each skill the project's ``mappings`` use, in
    order, each once, where it's first named."""
    added = task_skills or [name for mapping in mappings for name in mapping.use]
    return tuple(dict.fromkeys((*role_skills, *added)))


def delivered_skills(
    role_skills: Sequence[str],
    task_skills: Sequence[str],
    mappings: Sequence[SkillMapping],
    library: SkillLibrary,
) -> tuple[str, ...]:
    """The skills delivered to an agent, by name: those it's given, as :func:`given_skills` says
    from its role's ``role_skills``, its task's ``task_skills`` and the project's ``mappings``,
    that the ``library`` has, in order."""
    given = given_skills(role_skills, task_skills, mappings)
    return tuple(name for name in given if name in library.skills)


def skill_instructions(
    role_skills: Sequence[str],
    task_skills: Sequence[str],
    mappings: Sequence[SkillMapping],
    library: SkillLibrary,
) -> str:
    """What follows a role's instructions to deliver an agent its skills, those
    :func:`delivered_skills` says from its role's ``role_skills``, its task's ``task_skills``, the
    project's ``mappings`` and the ``library``: under a :data:`SKILLS_HEADING`, the
    :data:`READING_LINE`, then each skill's body under its name, and, where the mappings gave
    skills, as they do a task without any of its own, a line for each saying where its skills
    apply. Empty for an agent delivered none."""
    delivered = delivered_skills(role_skills, task_skills, mappings, library)
    if not delivered:
        return ""
    sections = (_skill_section(library.skills[name]) for name in delivered)
    mapped = () if task_skills else _where_mapped(mappings)
    return "\n\n".join((SKILLS_HEADING, READING_LINE, *sections, *mapped))


def _skill_section(skill: Skill) -> str:
    """The section delivering ``skill``: its name as a heading, then its body, without the blank
    space ending it."""
    return f"### {skill.name}\n\n{skill.body}".rstrip()


def _where_mapped(mappings: Sequence[SkillMapping]) -> tuple[str, ...]:
    """The paragraph saying where the skills of each of the ``mappings`` apply, a line for each;
    none without mappings."""
    return ("\n".join(_mapping_line(mapping) for mapping in mappings),) if mappings else ()


def _mapping_line(mapping: SkillMapping) -> str:
    """The line saying the skills ``mapping`` uses apply under its paths, each in backticks."""
    skills = ", ".join(f"`{name}`" for name in mapping.use)
    paths = ", ".join(f"`{path}`" for path in mapping.paths)
    verb = "applies" if len(mapping.use) == 1 else "apply"
    return f"{skills} {verb} under {paths}."


def given_skill_file(
    given: Sequence[str], library: SkillLibrary, skill: str, path: str
) -> SkillFile | str:
    """The file at ``path``, relative to its folder, of the skill ``skill`` in the ``library``,
    for an agent given the skills ``given``; or why it's refused: the agent was given none, or
    wasn't given that one, or the skill has no file at that path, as one with ``..`` or an
    absolute one never is. A skill given that the ``library`` lacks, as one added to the project
    since it was read does, isn't the agent's to read."""
    readable = [name for name in given if name in library.skills]
    if not readable:
        return NO_SKILLS
    if skill not in readable:
        return f"You weren't given the skill {skill}; your skills are {', '.join(readable)}."
    files = library.skills[skill].files
    if path not in files:
        return f"{skill} has no file {path}; its files are {', '.join(sorted(files))}."
    return files[path]


def check_role_skills(name: str, skills: Sequence[str], library: SkillLibrary) -> None:
    """Raise :class:`ConfigError` naming the first of the ``skills`` role ``name`` names that
    isn't in the ``library``, if any, as :func:`unknown_skill` does, under the key
    ``roles.<name>.skills``."""
    unknown = [skill for skill in skills if skill not in library.skills]
    if unknown:
        raise ConfigError(unknown_skill(f"roles.{name}.skills", unknown[0], library))


def check_skill_mappings(
    mappings: Sequence[SkillMapping],
    library: SkillLibrary,
    present: Collection[str],
    escaping: Collection[str],
) -> None:
    """Raise :class:`ConfigError` naming the first problem with the ``mappings``, entry by entry,
    each numbered from 1, and in each its paths in order, then its skills: a path that's empty,
    holds a NUL byte, isn't relative or leaves the project, by its ``..`` or as one of the paths
    ``escaping``, whose links lead out of it, one that isn't ``present``, among the paths in the
    project, or a skill that isn't in the ``library``."""
    problem = next(_mapping_problems(mappings, library, present, escaping), None)
    if problem is not None:
        raise ConfigError(problem)


def _mapping_problems(
    mappings: Sequence[SkillMapping],
    library: SkillLibrary,
    present: Collection[str],
    escaping: Collection[str],
) -> Iterator[str]:
    """The problems :func:`check_skill_mappings` finds with the ``mappings``, in order."""
    for number, mapping in enumerate(mappings, start=1):
        yield from _path_problems(f"skills.{number}.paths", mapping.paths, present, escaping)
        key = f"skills.{number}.use"
        yield from (
            unknown_skill(key, name, library) for name in mapping.use if name not in library.skills
        )


def _path_problems(
    key: str, paths: Sequence[str], present: Collection[str], escaping: Collection[str]
) -> Iterator[str]:
    """The problems with the ``paths`` set under ``key``, in order: each that's empty, holds a NUL
    byte, isn't relative or leaves the project, by its ``..`` or as one of the paths ``escaping``,
    and each that isn't ``present``, among the paths in the project."""
    for path in paths:
        if not inside_project(path) or path in escaping:
            yield f"{key}: {OUTSIDE_PROJECT}"
        elif path not in present:
            yield f"{key} names {path}, which isn't in the project."
