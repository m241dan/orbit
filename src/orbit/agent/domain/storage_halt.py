"""Why a session halted when a write to the store failed, and what the user can do about it."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class FailedWrite(StrEnum):
    """Which write to the store failed."""

    EVENT_LOG = "the event log"
    SESSION_RECORD = "the session record"
    REVIEW_COUNT = "the review count"


class StorageErrorKind(StrEnum):
    """What orbit makes of a store's error."""

    DISK_FULL = "disk full"
    READ_ONLY = "read-only or permission denied"
    LOCKED = "locked beyond the busy timeout"
    MISSING = "file missing"
    UNKNOWN = "not recognised"


@dataclass(frozen=True)
class StorageFault:
    """A failed write: which ``write``, to the ``database`` file, failing with ``error`` as the
    store reported it, of the ``kind`` orbit made of it."""

    write: FailedWrite
    database: str
    error: str
    kind: StorageErrorKind


SeqSpan = tuple[int, int]
"""The first and last ``seq`` of some events."""

_CAUSES = {
    StorageErrorKind.DISK_FULL: "The disk is full: free space on the disk holding {database}.",
    StorageErrorKind.READ_ONLY: (
        "The database can't be written: check the owner and mode of the state directory holding"
        " {database}, and of the files in it."
    ),
    StorageErrorKind.LOCKED: (
        "The database stayed locked beyond the busy timeout: another process holds the"
        " database; close it, or wait for it to finish."
    ),
    StorageErrorKind.MISSING: (
        "The database file is missing: the state directory holding {database} was moved or"
        " removed; put it back."
    ),
    StorageErrorKind.UNKNOWN: "Orbit doesn't recognise this error, so it can't say what caused it.",
}

NEITHER_PLACE = (
    "neither the state directory's fallback/ nor the repository's .git/orbit/fallback/ could be"
    " written"
)
RESUME = 'Choose "Try again" to resume saving once that\'s fixed, or restart orbit.'


def seq_span(seqs: Iterable[int]) -> SeqSpan | None:
    """The first and last of ``seqs``; none when there are none."""
    ordered = sorted(seqs)
    return (ordered[0], ordered[-1]) if ordered else None


def halting_message(write: FailedWrite) -> str:
    """What a halt says while its explanation is still being made: saving ``write`` failed."""
    return f"Saving {write} failed, so orbit is stopping the session."


def _unsaved(unsaved: SeqSpan | None, written_to: str | None) -> str:
    """Where the events ``unsaved``, if any, went: ``written_to``, or nowhere when that's none."""
    if unsaved is None and written_to is None:
        return f"No events were left unsaved, but {NEITHER_PLACE}, so later events aren't kept."
    if unsaved is None:
        return f"No events were left unsaved; later events are written to {written_to}."
    first, last = unsaved
    events = f"The unsaved events, seq {first} to {last},"
    if written_to is None:
        return f"{events} weren't written anywhere: {NEITHER_PLACE}."
    return f"{events} were written to {written_to}, as are later events."


def storage_halt_explanation(
    fault: StorageFault, unsaved: SeqSpan | None, written_to: str | None
) -> str:
    """Why the session halted on ``fault``, and what to do: which write failed, the database and
    its error, the cause orbit diagnosed and its fix, the ``unsaved`` events' ``seq``s (if any)
    and the fallback file ``written_to`` (none when neither place could be written), that what
    was saved is intact, and how to resume."""
    return "\n".join(
        (
            f"Orbit stopped this session: saving {fault.write} to {fault.database} failed.",
            f"The error ({fault.kind}): {fault.error}",
            _CAUSES[fault.kind].format(database=fault.database),
            _unsaved(unsaved, written_to),
            "What was saved is intact, up to the last saved event.",
            RESUME,
        )
    )
