"""Builders for saved sessions and the hosts that switch between them."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from orbit.agent import Verification, VerificationStep, WorkspaceHost
from orbit.agent.domain.sessions import SessionRecord
from orbit.protocol import (
    ActivateSession,
    ErrorOccurred,
    Event,
    EventEnvelope,
    NewSession,
    Notice,
    TaskState,
)
from tests.agent_fakes import (
    FakeStore,
    FakeTree,
    Script,
    ScriptedLauncher,
    Steps,
    Ticks,
    agents_for,
    send,
    until_marked,
)

YESTERDAY = datetime(2026, 10, 16, 18, 2, tzinfo=UTC)


EARLIER = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def saved(session_id: str, *tasks: TaskState, **fields: Any) -> SessionRecord:
    """A record saved as ``session_id`` holding ``tasks``; ``fields`` override the rest."""
    record = SessionRecord(
        id=session_id, name=session_id, created=EARLIER, last_active=YESTERDAY, tasks=tasks
    )
    return record.model_copy(update=fields)


def store_with(*records: SessionRecord) -> FakeStore:
    store = FakeStore()
    store.saved = list(records)
    return store


def switching(
    store: FakeStore, *scripts: Script, reviews: bool = True, **session: Any
) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session saved in ``store``, as ``session-1`` then ``session-2`` and so on, whose tasks
    run through agents playing ``scripts``, reviewing them if ``reviews``; ``session`` goes to
    :class:`WorkspaceHost`."""
    tree = FakeTree() if reviews else None
    agents, launcher = agents_for(*scripts, tree=tree, review=reviews)
    ids = iter(f"session-{n}" for n in range(1, 100))
    built = WorkspaceHost(
        agents=agents, store=store, clock=Ticks(), new_id=lambda: next(ids), **session
    )
    return built, launcher


def last_of[E](kind: type[E], events: list[Event]) -> E:
    return [e for e in events if isinstance(e, kind)][-1]


def messages(kind: type[Notice] | type[ErrorOccurred], events: list[Event]) -> list[str]:
    return [e.message for e in events if isinstance(e, kind)]


async def opened(session: WorkspaceHost, command: ActivateSession | NewSession) -> list[Event]:
    """The events from sending ``command`` until its session is opened, and its notes."""
    await send(session, command)
    return await until_marked(session)  # Marks the end of the notes that follow.


def failing_startup(
    store: FakeStore, gate: asyncio.Event | None = None, picking: bool = True
) -> WorkspaceHost:
    """A session, for the user to pick if ``picking``, whose startup check fails, once ``gate``
    opens if given, adding a draft to fix it."""
    lint = VerificationStep(name="Lint", command=("ruff", "check"))
    steps = Steps(False)
    steps.gate = gate
    session, _ = switching(
        store,
        reviews=False,
        verification=Verification((lint,), steps),
        check_at_startup=True,
        pick_session=picking,
    )
    return session


OPEN = "session-1"
"""The id of the session a host opens first, its ids counting up from ``session-1``."""


def store_logging(*logged: EventEnvelope) -> FakeStore:
    """A store whose log holds ``logged``."""
    store = FakeStore()
    store.logged = list(logged)
    return store


def review_count_json(count: int) -> str:
    """``count`` commits since the last super review as an older orbit's ``reviews.json`` text,
    for :func:`parse_review_count` to read back."""
    return json.dumps({"since_super": count})
