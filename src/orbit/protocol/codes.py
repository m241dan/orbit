"""The codes a refusal or an error carries, as described in ``docs/protocol.md``."""

NOT_CONFIGURED = "not_configured"
"""A refusal's code: what the command needs isn't set up, such as agents or verification."""

UNKNOWN = "unknown"
"""A refusal's code: the command names something there's none of, such as a task or a lane."""

BUSY = "busy"
"""A refusal's code: what the command acts on is running, so it can't be done now."""

INVALID = "invalid"
"""A refusal's code: the command can't be done as given, such as input to a trigger that takes
none."""

LOCKED = "locked"
"""A refusal's code: another orbit holds what the command acts on."""

WRONG_SESSION = "wrong_session"
"""A refusal's code: the command is for a session that isn't open."""

INCOMPATIBLE = "incompatible"
"""A refusal's code: a ``Hello`` named a protocol version this orbit doesn't speak; nothing more
is sent on that stream."""

AGENT_FAILED = "agent_failed"
"""An error's code: an agent's run or turn failed, or it exited without doing its task."""

TIMED_OUT = "timed_out"
"""An error's code: an agent ran out of time."""

GIT_FAILED = "git_failed"
"""An error's code: a git command or a worktree failed."""

SAVE_FAILED = "save_failed"
"""An error's code: saving the session failed."""

CHECKS_EXHAUSTED = "checks_exhausted"
"""An error's code: a check still failed after every fix attempt."""

INTERNAL = "internal"
"""An error's code: orbit itself failed unexpectedly; also a refusal's code when orbit failed
handling the command."""

REFUSAL_CODES = (NOT_CONFIGURED, UNKNOWN, BUSY, INVALID, LOCKED, WRONG_SESSION, INCOMPATIBLE)
"""The codes a ``CommandRefused`` carries, as described in ``docs/protocol.md``; it may also
carry ``INTERNAL`` when orbit failed handling the command."""

ERROR_CODES = (AGENT_FAILED, TIMED_OUT, GIT_FAILED, SAVE_FAILED, CHECKS_EXHAUSTED, INTERNAL)
"""The codes an ``ErrorOccurred`` carries, besides a refusal code where it fits, as described
in ``docs/protocol.md``."""
