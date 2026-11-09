"""Wording both the backend and the frontends share, on the standard library alone."""

from collections.abc import Sequence


def join_in_words(words: Sequence[str]) -> str:
    """``words`` joined in English, as ``a``, ``a and b`` or ``a, b and c``; empty for none."""
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"
