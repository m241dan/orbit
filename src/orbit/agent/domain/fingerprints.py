"""Whether two repositories' fingerprints are the same repository wherever it's moved, and the
notes on a workspace re-attached to its moved repository.

A fingerprint is a repository's root commits' hashes and its ``origin`` remote's URL, empty
without one, joined by spaces."""

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_SCP = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.*)$")


def normalise_origin(url: str) -> str:
    """``url`` as its host, in lowercase, and path, without its user, port, a trailing ``.git``
    or slashes, so the same remote reached over SSH, scp-style or HTTPS is the same."""
    host, path = _host_and_path(url.strip())
    path = path.strip("/").removesuffix(".git").strip("/")
    return "/".join(part for part in (host.lower(), path) if part)


def _host_and_path(url: str) -> tuple[str, str]:
    """The host, if any, and the path of ``url``, a URL, scp-style address or local path."""
    if _SCHEME.match(url):
        parts = urlsplit(url)
        return parts.hostname or "", parts.path
    scp = _SCP.match(url)
    return ("", url) if scp is None else (scp[1], scp[2])


def fingerprints_match(a: str, b: str) -> bool:
    """Whether the fingerprints ``a`` and ``b`` are the same repository: the same root commits,
    and the same normalised ``origin`` or none on one side."""
    roots_a, origin_a = _parsed(a)
    roots_b, origin_b = _parsed(b)
    if not roots_a or roots_a != roots_b:
        return False
    return not origin_a or not origin_b or normalise_origin(origin_a) == normalise_origin(origin_b)


def _parsed(fingerprint: str) -> tuple[frozenset[str], str]:
    """The root commits and the ``origin`` URL, empty if none, of ``fingerprint``."""
    *roots, origin = fingerprint.split(" ")
    return frozenset(roots), origin


def reattached_note(old: str, new: str) -> str:
    """The note on a workspace at ``old`` re-attached to its repository, moved to ``new``."""
    return f"Re-attached the sessions of {old}, which moved to {new}."


def lookalikes_note(new: str, olds: Sequence[str]) -> str:
    """The note on the repository at ``new`` registered as a new workspace, as it looks like
    those of several workspaces, at ``olds``, whose repositories have gone."""
    return f"{new} looks like {', '.join(olds)}; started it as a new workspace."
