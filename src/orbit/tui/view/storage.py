"""How a session halted because storage failed is shown."""

HALT_TITLE = "Session stopped: storage failed"
HALT_HINT = "enter try again · esc close"

# The writes a halt's explanation names, as the status line names them.
_WRITES = ("event log", "session record", "review count")


def halt_status(explanation: str) -> str:
    """The status line shown by the prompt while halted, naming the write that failed as the
    first line of ``explanation`` names it, or storage when it names none."""
    first = explanation.partition("\n")[0]
    failed = next((write for write in _WRITES if write in first), "storage")
    return f"Session stopped: {failed} failed; see the transcript"
