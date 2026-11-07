"""Globs over repository-relative git paths: whether one matches a path, with ``*`` within one
directory and ``**`` for any number of them, and whether one can match a git path at all."""

from fnmatch import fnmatchcase

ANY_DIRECTORIES = "**"
"""The part of a glob that stands for any number of directories, none included."""

_GIT_PATH_NON_PARTS = frozenset({"", ".", ".."})
"""The parts, between slashes, that a repository-relative git path never has."""


def matchable(glob: str) -> bool:
    """Whether ``glob`` can match a git path at all: none of its parts, split at its slashes,
    is one a git path never has, as a leading or trailing ``/``, ``//``, ``.`` or ``..`` make."""
    return _GIT_PATH_NON_PARTS.isdisjoint(glob.split("/"))


def unmatchable_glob(glob: str) -> str:
    """Why ``glob``, a glob :func:`matchable` refuses, is refused, and how to write a
    directory instead."""
    return (
        f'"{glob}" can never match a git path, which neither starts nor ends with "/" and has'
        ' no empty, "." or ".." part; write a directory as "dir/**".'
    )


def matches_path(pattern: str, path: str) -> bool:
    """Whether ``path``, a repository-relative git path, matches ``pattern``, a glob matched
    with :func:`~fnmatch.fnmatchcase` directory by directory, so ``*`` stays within one, but for
    a part that's just ``**``, which stands for any number of directories, none included."""
    return _matches_parts(tuple(pattern.split("/")), tuple(path.split("/")))


def _matches_parts(patterns: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    """Whether the ``parts`` of a path, split at its slashes, match those of a glob,
    ``patterns``, as :func:`matches_path` says."""
    if not patterns:
        return not parts
    first, rest = patterns[0], patterns[1:]
    if first == ANY_DIRECTORIES:
        return any(_matches_parts(rest, parts[skipped:]) for skipped in range(len(parts) + 1))
    return bool(parts) and fnmatchcase(parts[0], first) and _matches_parts(rest, parts[1:])
