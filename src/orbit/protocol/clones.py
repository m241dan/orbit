"""The rules of a clone that a server and its frontends both follow: what's a repository's URL
rather than a path, the name a clone is given, and how a clone's failure is told."""

import re

_URL = re.compile(r"(?:https|ssh|file)://\S+|[^@/:\s]+@[^@/:\s]+:\S+")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def looks_like_url(text: str) -> bool:
    """Whether ``text`` is an ``https://``, ``ssh://`` or ``file://`` URL or a scp-style
    ``user@host:path``, rather than a path."""
    return _URL.fullmatch(text) is not None


def clone_name(url: str) -> str:
    """The name of the directory a clone of ``url`` goes to: its last part, without ``.git``,
    with what isn't a letter, digit, dot, dash or underscore made a dash, and no leading or
    trailing dots or dashes; ``repository`` if nothing's left."""
    last = re.split(r"[/:]", url.rstrip("/"))[-1]
    name = _UNSAFE.sub("-", last.removesuffix(".git")).strip(".-")
    return name or "repository"


def clone_failure_message(url: str, detail: str) -> str:
    """What's said when the clone of ``url`` fails, as ``detail`` says why:
    ``Couldn't clone <url>: <detail>``."""
    return f"{_failure_prefix(url)}{detail}"


def failed_clone(message: str, url: str) -> bool:
    """Whether ``message`` says the clone of ``url`` failed, as
    :func:`clone_failure_message` says it."""
    return message.startswith(_failure_prefix(url))


def _failure_prefix(url: str) -> str:
    """What every message saying the clone of ``url`` failed starts with."""
    return f"Couldn't clone {url}: "
