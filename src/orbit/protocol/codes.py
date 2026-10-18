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
"""A refusal's code: the command is for a session other than the one its connection views."""

NOT_ACTIVE = "not_active"
"""A refusal's code: the connection views a session that isn't the workspace's open one, so the
command, which would change or run something there, can't be done in it."""

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

STORAGE_FAILED = "storage_failed"
"""A refusal's and an error's code: a write to the store failed, so the session halted; the
message explains the failure and how to resume."""

CHECKS_EXHAUSTED = "checks_exhausted"
"""An error's code: a check still failed after every fix attempt."""

INTERNAL = "internal"
"""An error's code: orbit itself failed unexpectedly; also a refusal's code when orbit failed
handling the command."""

REFUSAL_CODES = (
    NOT_CONFIGURED,
    UNKNOWN,
    BUSY,
    INVALID,
    LOCKED,
    WRONG_SESSION,
    NOT_ACTIVE,
    INCOMPATIBLE,
    STORAGE_FAILED,
)
"""The codes a ``CommandRefused`` carries, as described in ``docs/protocol.md``; it may also
carry ``INTERNAL`` when orbit failed handling the command."""

ERROR_CODES = (
    AGENT_FAILED,
    TIMED_OUT,
    GIT_FAILED,
    SAVE_FAILED,
    STORAGE_FAILED,
    CHECKS_EXHAUSTED,
    INTERNAL,
)
"""The codes an ``ErrorOccurred`` carries, besides a refusal code where it fits, as described
in ``docs/protocol.md``."""
