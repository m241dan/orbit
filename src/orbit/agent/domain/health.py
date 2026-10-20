"""The health checks ``orbit serve`` answers: whether it's ready, and how ``/readyz`` says so in
the minimal form of the IETF health-check draft."""

from typing import Literal

Readiness = Literal["pass", "read_only", "starting", "shutting_down", "storage_halted"]
"""Whether the server is ready, and if not, why."""


def readiness(*, accepting: bool, stopping: bool, halted: bool, read_only: bool) -> Readiness:
    """How ready a server is: shutting down once it's ``stopping``, starting until it's
    ``accepting``, then halted while storage is ``halted``, read-only while another orbit owns
    the repository, and otherwise passing."""
    if stopping:
        return "shutting_down"
    if not accepting:
        return "starting"
    if halted:
        return "storage_halted"
    return "read_only" if read_only else "pass"


def ready_answer(state: Readiness) -> tuple[int, dict[str, str]]:
    """The HTTP status and body ``/readyz`` answers in ``state``: passing, a warning while
    read-only, and otherwise failing with the reason."""
    if state == "pass":
        return 200, {"status": "pass"}
    if state == "read_only":
        return 200, {"status": "warn", "output": state}
    return 503, {"status": "fail", "output": state}
