"""What orbit asks of the runtime agents run on: what it can do, and the failures it tells in
its agents' errors."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from orbit.protocol import Effort, ToolAccess

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
