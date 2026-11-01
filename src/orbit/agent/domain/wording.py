"""The backend's wording shared across its messages: a noun made plural unless its count is
one."""


def noun(count: int, singular: str, plural: str | None = None) -> str:
    """``singular``, or ``plural`` (``singular`` and an s by default) unless ``count`` is one:
    ``commit``, ``commits``."""
    return singular if count == 1 else plural or f"{singular}s"


def counted(count: int, singular: str, plural: str | None = None) -> str:
    """``count`` and its :func:`noun`: ``1 commit``, ``2 commits``."""
    return f"{count} {noun(count, singular, plural)}"
