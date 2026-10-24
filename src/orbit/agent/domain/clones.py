"""Cloning a repository to serve it: what ``AddWorkspace`` may be given, the directory a clone
goes to, git's progress as it clones, and what's said when a clone fails."""

import itertools
import re
from collections.abc import Iterator, Sequence
from urllib.parse import unquote, urlsplit

from orbit.agent.domain.refusals import Refused
from orbit.protocol import (
    INVALID,
    WorkspaceCloning,
    clone_failure_message,
    clone_name,
    looks_like_url,
)

AUTH_HINT = (
    "The server's account needs access to this repository: an SSH key or a git credential helper."
)
"""What a clone that failed for want of credentials adds to git's message."""

_PROGRESS = re.compile(
    r"(?:remote: )?(?P<phase>[A-Z][a-z]+(?: [a-z]+)*): +(?:(?P<percent>\d{1,3})% )?\(?\d"
)
_AUTH = re.compile(
    r"permission denied|authentication failed|could not read (?:username|password)"
    r"|terminal prompts disabled|host key verification failed",
    re.IGNORECASE,
)
_LAST_LINES = 5


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


def local_clone_source(url: str) -> str | None:
    """The path on the server's host that cloning ``url`` copies: ``url`` itself if it's an
    absolute path, or a ``file://`` URL's path, percent-decoded, its host ignored as git ignores
    it; none for a remote URL."""
    if url.startswith("/"):
        return url
    if url.startswith("file://"):
        return unquote(urlsplit(url).path)
    return None


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


def clone_failure(url: str, said: Sequence[str]) -> str:
    """What's said when the clone of ``url`` fails: git's last lines of those it ``said``,
    other than its progress, and :data:`AUTH_HINT` when they look like it lacked credentials."""
    message = "\n".join(said[-_LAST_LINES:]) or "git said nothing."
    failure = clone_failure_message(url, message)
    if _AUTH.search(message):
        return f"{failure}\n{AUTH_HINT}"
    return failure
