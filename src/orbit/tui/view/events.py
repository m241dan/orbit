"""Which events are worth rendering."""

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from orbit.protocol import (
    Event,
    ReviewStatus,
    SettingsChanged,
    TaskMerged,
    TasksChanged,
    TaskStarted,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)

# Snapshot events a later one of the same type replaces, unless one of these events, which
# read the state the snapshot sets, lies between them.
SUPERSEDED_UNLESS: Mapping[type[Event], tuple[type[Event], ...]] = MappingProxyType(
    {
        TasksChanged: (
            TaskStarted,
            VerificationStarted,
            VerificationSkipped,
            VerificationFinished,
            TaskMerged,
        ),
        VerificationChanged: (VerificationStarted, VerificationFinished),
        ReviewStatus: (),
        SettingsChanged: (),
    }
)


def coalesce(events: Sequence[Event]) -> list[Event]:
    """The ``events`` to render, in order: without the snapshots a later one replaces before
    anything reads them."""
    kept: list[Event] = []
    superseded: set[type[Event]] = set()
    for event in reversed(events):
        kind = type(event)
        if kind not in superseded:
            kept.append(event)
        if kind in SUPERSEDED_UNLESS:
            superseded.add(kind)
        superseded -= {s for s in superseded if kind in SUPERSEDED_UNLESS[s]}
    kept.reverse()
    return kept
