"""Cloning a repository to serve it: what ``AddWorkspace`` may be given, the directory a clone
goes to, git's progress as it clones, and what's said when a clone fails."""

import itertools
import re
from collections.abc import Iterator, Sequence

from orbit.agent.domain.refusals import Refused
from orbit.protocol import INVALID, WorkspaceCloning

AUTH_HINT = (
    "The server's account needs access to this repository: an SSH key or a git credential helper."
)
"""What a clone that failed for want of credentials adds to git's message."""

_URL = re.compile(r"(?:https|ssh|file)://\S+|[^@/:\s]+@[^@/:\s]+:\S+")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_PROGRESS = re.compile(
    r"(?:remote: )?(?P<phase>[A-Z][a-z]+(?: [a-z]+)*): +(?:(?P<percent>\d{1,3})% )?\(?\d"
)
_AUTH = re.compile(
    r"permission denied|authentication failed|could not read (?:username|password)"
    r"|terminal prompts disabled|host key verification failed",
    re.IGNORECASE,
)
_LAST_LINES = 5


def looks_like_url(text: str) -> bool:
    """Whether ``text`` is an ``https://``, ``ssh://`` or ``file://`` URL or a scp-style
    ``user@host:path``, rather than a path."""
    return _URL.fullmatch(text) is not None


def added_url(path: str | None, url: str | None) -> str | None:
    """The ``url`` an ``AddWorkspace`` given ``path`` or ``url`` clones; none when it adds the
    ``path``. Refused as ``invalid`` unless exactly one is given, or for a ``url`` that's
    neither a URL :func:`looks_like_url` accepts nor an absolute path."""
    if (path is None) == (url is None):
        raise Refused(INVALID, "Give AddWorkspace a path or a URL, one of them.")
    if url is not None and not (looks_like_url(url) or url.startswith("/")):
        raise Refused(
            INVALID,
            f"Can't clone {url}: give an https://, ssh:// or file:// URL, user@host:path or an"
            " absolute path.",
        )
    return url


def clone_name(url: str) -> str:
    """The name of the directory a clone of ``url`` goes to: its last part, without ``.git``,
    with what isn't a letter, digit, dot, dash or underscore made a dash, and no leading or
    trailing dots or dashes; ``repository`` if nothing's left."""
    last = re.split(r"[/:]", url.rstrip("/"))[-1]
    name = _UNSAFE.sub("-", last.removesuffix(".git")).strip(".-")
    return name or "repository"


def clone_names(name: str) -> Iterator[str]:
    """``name``, then ``name-2``, ``name-3`` and so on, for when it's taken."""
    yield name
    yield from (f"{name}-{number}" for number in itertools.count(2))


def clone_progress(url: str, line: str) -> WorkspaceCloning | None:
    """The progress of the clone of ``url`` that git's ``line`` reports, such as
    ``Receiving objects:  45% (450/1000)``; none if it reports none."""
    found = _PROGRESS.match(line)
    if found is None:
        return None
    percent = found["percent"]
    return WorkspaceCloning(
        url=url,
        name=clone_name(url),
        phase=found["phase"],
        percent=None if percent is None else int(percent),
    )


def clone_served(url: str, workspace: str) -> WorkspaceCloning:
    """The last news of the clone of ``url``: it's done, served as the workspace
    ``workspace``."""
    return WorkspaceCloning(
        url=url, name=clone_name(url), phase="Done", percent=100, workspace=workspace
    )


def couldnt_clone(url: str, why: str) -> str:
    """What's said when the clone of ``url`` fails for the reason ``why``:
    ``Couldn't clone <url>: <why>``."""
    return f"Couldn't clone {url}: {why}"


def clone_failure(url: str, said: Sequence[str]) -> str:
    """What's said when the clone of ``url`` fails: git's last lines of those it ``said``,
    other than its progress, and :data:`AUTH_HINT` when they look like it lacked credentials."""
    message = "\n".join(said[-_LAST_LINES:]) or "git said nothing."
    failure = couldnt_clone(url, message)
    if _AUTH.search(message):
        return f"{failure}\n{AUTH_HINT}"
    return failure
