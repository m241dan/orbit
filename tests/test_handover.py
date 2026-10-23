"""Handing the repository over: what answering the offer of it does, and in what order."""

from collections.abc import Sequence

import pytest

from orbit.agent.application.handover import OWNED_NOTE, Handover, HandoverParts
from orbit.agent.application.recovery import RecoveryFailed
from orbit.agent.domain.refusals import Refused
from orbit.protocol import (
    INTERNAL,
    INVALID,
    ErrorOccurred,
    Event,
    Notice,
    OwnershipDeclined,
    OwnershipTaken,
    TakeOwnership,
)

COMMAND_ID = "c1"


class Effects:
    """What a handover did, in order, through the fakes of its parts."""

    def __init__(self) -> None:
        self.log: list[object] = []
        self.answerable = True
        self.notes: Sequence[str] = ()
        self.recovery_failure: RecoveryFailed | None = None
        self.catalog_failure: Exception | None = None
        self.own_failure: Exception | None = None

    def answer(self, take: bool) -> None:
        if not self.answerable:
            raise Refused(INVALID, "No offer.")
        self.log.append(("answer", take))

    def taking(self) -> None:
        self.log.append("taking")

    def taken(self) -> None:
        self.log.append("taken")

    async def own(self, command_id: str) -> None:
        self.log.append(("own", command_id))
        if self.own_failure is not None:
            raise self.own_failure

    async def put(self, item: Event) -> None:
        self.log.append(item)

    def put_nowait(self, item: Event) -> None:
        self.log.append(item)

    def owned(self) -> None:
        self.log.append("owned")

    async def publish_catalog(self) -> None:
        self.log.append("catalog")
        if self.catalog_failure is not None:
            raise self.catalog_failure

    async def recover(self) -> Sequence[str]:
        self.log.append("recover")
        if self.recovery_failure is not None:
            raise self.recovery_failure
        return self.notes

    async def recount(self) -> None:
        self.log.append("recount")

    def kick(self) -> None:
        self.log.append("kick")


def handover_on(effects: Effects) -> Handover:
    parts = HandoverParts(
        effects,
        effects,
        effects,
        effects.owned,
        effects.publish_catalog,
        effects.recover,
        effects.recount,
        effects.kick,
    )
    return Handover(parts)


async def test_taking_the_repository_announces_recovers_recounts_then_runs_the_queue() -> None:
    effects = Effects()
    effects.notes = ("Recovered 3 events.",)

    await handover_on(effects).answer(TakeOwnership(take=True), COMMAND_ID)

    assert effects.log == [
        ("answer", True),
        "taking",
        "owned",
        OwnershipTaken(),
        Notice(message=OWNED_NOTE),
        "catalog",
        "recover",
        Notice(message="Recovered 3 events."),
        "recount",
        ("own", COMMAND_ID),
        "kick",
        "taken",
    ]


async def test_declining_the_repository_publishes_only_that_it_was_declined() -> None:
    effects = Effects()

    await handover_on(effects).answer(TakeOwnership(take=False), COMMAND_ID)

    assert effects.log == [("answer", False), OwnershipDeclined()]


async def test_answering_with_no_offer_open_is_refused_and_does_nothing() -> None:
    effects = Effects()
    effects.answerable = False

    with pytest.raises(Refused):
        await handover_on(effects).answer(TakeOwnership(take=True), COMMAND_ID)

    assert effects.log == []


async def test_a_failed_recovery_is_reported_as_internal_and_the_takeover_goes_on() -> None:
    effects = Effects()
    failure = RecoveryFailed(".orbit/fallback/s1.jsonl", "Not JSON")
    effects.recovery_failure = failure

    await handover_on(effects).answer(TakeOwnership(take=True), COMMAND_ID)

    recovered_at = effects.log.index("recover")
    assert effects.log[recovered_at + 1 :] == [
        ErrorOccurred(code=INTERNAL, message=str(failure)),
        "recount",
        ("own", COMMAND_ID),
        "kick",
        "taken",
    ]


async def test_a_failed_announcement_still_runs_the_queue_ends_the_takeover_and_raises() -> None:
    effects = Effects()
    effects.catalog_failure = RuntimeError("store gone")

    with pytest.raises(RuntimeError, match="store gone"):
        await handover_on(effects).answer(TakeOwnership(take=True), COMMAND_ID)

    assert effects.log[effects.log.index("catalog") + 1 :] == [("own", COMMAND_ID), "kick", "taken"]


async def test_reviews_are_kicked_even_when_running_the_queue_fails() -> None:
    effects = Effects()
    effects.own_failure = RuntimeError("outbox closed")

    with pytest.raises(RuntimeError, match="outbox closed"):
        await handover_on(effects).answer(TakeOwnership(take=True), COMMAND_ID)

    assert effects.log[-3:] == [("own", COMMAND_ID), "kick", "taken"]
