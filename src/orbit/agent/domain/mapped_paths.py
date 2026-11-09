"""The project's mapped code: the paths its ``[[skills]]`` entries map, and which files count as
code under them, written once so the super review's sample and the reviews a landing triggers
agree."""

import posixpath
from collections.abc import Collection, Iterable

from orbit.agent.domain.skills import SkillMapping

CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".cs",
        ".swift",
        ".rb",
    }
)
"""The extensions of the files of source code, those a super review may sample."""


def list_mapped_paths(mappings: Iterable[SkillMapping]) -> tuple[str, ...]:
    """The paths the ``mappings`` name, as written, each mapping's in turn."""
    return tuple(path for mapping in mappings for path in mapping.paths)


def is_mapped_code(path: str, mapped_paths: Collection[str]) -> bool:
    """Whether ``path`` is a file of source code, by its extension, under one of
    ``mapped_paths``, as :func:`is_mapped` says."""
    return is_code_file(path) and is_mapped(path, mapped_paths)


def is_mapped(path: str, mapped_paths: Collection[str]) -> bool:
    """Whether ``path`` is one of ``mapped_paths``, as written or normalised, or inside one:
    any path is when there are none, and the project's root, ``.``, holds every path."""
    roots = [posixpath.normpath(mapped) for mapped in mapped_paths]
    return not roots or any(_inside(path, root) for root in roots)


def is_code_file(path: str) -> bool:
    """Whether ``path`` is a file of source code, by its extension."""
    return posixpath.splitext(path)[1] in CODE_EXTENSIONS


def _inside(path: str, root: str) -> bool:
    """Whether ``path`` is the normalised ``root``, or inside it: the project's root, ``.``,
    holds every path."""
    return root in (".", path) or path.startswith(f"{root}/")
