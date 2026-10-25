"""The runtimes agents run on, which a role names: those this version of orbit knows, what orbit
asks of one, what it can do, and the failures it tells in its agents' errors."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from orbit.protocol import Effort, ToolAccess

CLAUDE = "claude"
"""Claude Code, the runtime a role runs on unless it says otherwise."""

RUNTIME_NAMES = (CLAUDE,)
"""The runtimes this version of orbit knows, by name, in order."""


def knows_runtime(runtime: str) -> bool:
    """Whether this version of orbit knows the runtime ``runtime``."""
    return runtime in RUNTIME_NAMES


def known_runtimes() -> str:
    """What says which runtimes this version of orbit knows."""
    return f"this version of orbit knows the runtimes {', '.join(RUNTIME_NAMES)}."


def unknown_runtime(key: str, runtime: str) -> str:
    """Why the setting ``key``, giving the ``runtime`` this version of orbit doesn't know, is
    refused."""
    return f'{key} = "{runtime}": {known_runtimes()}'


def not_on_path(runtime: str, roles: Sequence[str], executable: str, install: str) -> str:
    """Why orbit can't start ``roles``' agents on ``runtime``: its ``executable`` isn't on
    PATH, so the user should do as ``install`` says or change their runtime."""
    return (
        f"Roles {', '.join(roles)} use the runtime {runtime}, but {executable} isn't on PATH."
        f" {install}, or set those roles' runtime to one you have."
    )


FailureKind = Literal["usage_limit", "api_unavailable"]
"""What stopped an agent: its runtime's usage limit, or its runtime's API not answering."""


@dataclass(frozen=True)
class Capabilities:
    """What a runtime can do: carry a conversation on (``resume``), report its usage limits
    (``usage``) and cap an agent's turns (``max_turns``), and the levels of ``tool_access`` and
    ``effort`` it can run an agent at."""

    resume: bool
    usage: bool
    max_turns: bool
    tool_access: frozenset[ToolAccess]
    effort: frozenset[Effort]


@dataclass(frozen=True)
class Failure:
    """An agent's error as its runtime tells it: the ``kind`` of failure, and when the limit
    it hit resets, ``resets_at``, if the error says."""

    kind: FailureKind
    resets_at: datetime | None = None
