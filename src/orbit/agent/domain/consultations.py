"""Consultations that couldn't produce a reply: why each was interrupted, as its asking role
hears it in place of a reply."""

from orbit.agent.domain.runtimes import Failure
from orbit.agent.domain.usage_limits import runtime_name

CANCELLED = "it was cancelled"
SWITCHED = "the session was switched"
RESTARTED = "orbit restarted"
DECLINED = "you declined it"
EXPIRED = "it expired waiting for approval"


def failed(error: str, failure: Failure | None, runtime: str) -> str:
    """Why a consultation whose turn on ``runtime`` failed with ``error`` was interrupted: the
    usage limit or the outage ``failure`` says ``error`` is, else the error itself."""
    if failure is None:
        return f"it failed: {error}"
    if failure.kind == "usage_limit":
        return f"{runtime_name(runtime)}'s usage limit was reached"
    return f"{runtime_name(runtime)}'s API wasn't answering"


def interrupted_message(consultant: str, title: str, why: str) -> str:
    """The message telling the asking role that ``consultant``'s consultation ``title`` was
    interrupted, for ``why``."""
    return f"The {consultant}'s consultation '{title}' was interrupted: {why}."
