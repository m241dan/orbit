"""Wording both the backend and the frontends share, on the standard library alone."""

from collections.abc import Sequence
from typing import Literal

Conjunction = Literal["and", "or"]
"""The word that joins the last of a list to the rest: all of them, or any one."""


def join_in_words(words: Sequence[str], *, conjunction: Conjunction = "and") -> str:
    """``words`` joined in English with ``conjunction``, as ``a``, ``a and b`` or ``a, b and c``;
    empty for none."""
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} {conjunction} {words[-1]}"


def word_unpassed(sets: Sequence[str]) -> str:
    """That the ``sets`` haven't passed: ``stress hasn't passed``, ``stress and soak haven't
    passed``."""
    verb = "hasn't" if len(sets) == 1 else "haven't"
    return f"{join_in_words(sets)} {verb} passed"


def word_unfixed(lenses: Sequence[str]) -> str:
    """That the findings the ``lenses`` raised aren't fixed: ``the finding from bugs-reviewer
    isn't fixed``, ``the findings from bugs-reviewer and security-reviewer aren't fixed``."""
    if len(lenses) == 1:
        return f"the finding from {lenses[0]} isn't fixed"
    return f"the findings from {join_in_words(lenses)} aren't fixed"


def word_undone(sets: Sequence[str], lenses: Sequence[str] = ()) -> str:
    """What releasing the holds on merges of the ``sets`` and of the findings the ``lenses``
    raised leaves undone: :func:`word_unpassed`, :func:`word_unfixed`, or both joined with
    ``and``."""
    clauses = [
        *([word_unpassed(sets)] if sets else []),
        *([word_unfixed(lenses)] if lenses else []),
    ]
    return " and ".join(clauses)
