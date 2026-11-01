"""The code files a super review reads besides those the commits since the last one changed: a
sample of the project's source, so every part of it comes up for review now and then."""

import posixpath
from collections.abc import Callable, Collection, Sequence

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
"""The extensions of the files a super review may sample: those of source code."""

type Choose = Callable[[Sequence[str], int], Sequence[str]]
"""Chooses so many of the paths given, each once, at random, as :func:`random.sample` does."""


def sample_files(
    tracked: Collection[str],
    mapped_paths: Collection[str],
    changed: Collection[str],
    size: int,
    choose: Choose,
) -> tuple[str, ...]:
    """Up to ``size`` of the ``tracked`` files, sorted, those ``choose`` picks of the eligible
    ones: with an extension in :data:`CODE_EXTENSIONS`, under one of ``mapped_paths`` when any
    are given (a mapped file itself, or a file inside a mapped folder) and not among the files
    ``changed``. All of them when fewer are eligible; none for a ``size`` of 0."""
    roots = [posixpath.normpath(path) for path in mapped_paths]
    unchanged = set(tracked) - set(changed)
    eligible = sorted(path for path in unchanged if _is_code(path) and _under(path, roots))
    return tuple(sorted(choose(eligible, min(size, len(eligible)))))


def _is_code(path: str) -> bool:
    """Whether ``path`` is a file of source code, by its extension."""
    return posixpath.splitext(path)[1] in CODE_EXTENSIONS


def _under(path: str, roots: Sequence[str]) -> bool:
    """Whether ``path`` is one of the normalised ``roots`` or inside one; any path is when there
    are none."""
    return not roots or any(_inside(path, root) for root in roots)


def _inside(path: str, root: str) -> bool:
    """Whether ``path`` is the normalised ``root``, or inside it: the project's root, ``.``,
    holds every path."""
    return root in (".", path) or path.startswith(f"{root}/")
