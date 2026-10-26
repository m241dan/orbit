"""The copies agents whose role works in a copy work in: fresh ones for a task's run, and a
conversational role's own, made at its first turn, kept across turns, switches and restarts, and
removed on a reset, or by age at startup and every hour."""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from orbit.agent.application.ports import ReadFailed, TreeFailed
from orbit.agent.application.role_copies import (
    OtherCopies,
    Recorded,
    RoleCopies,
    TreeCopies,
    copy_ttls,
    fresh_copy,
    saved_copies,
)
from orbit.agent.domain.sessions import SavedCopy, SessionRecord
from orbit.protocol import GIT_FAILED, ErrorOccurred, Event
from tests.agent_fakes import ROLES, FakeCopies, FakeStore, FakeTree

START = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class Clock:
    """A clock at :data:`START` until moved on."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def move(self, hours: float) -> None:
        self.now += timedelta(hours=hours)


class Ledger:
    """Role copies made by :attr:`made`, on :attr:`clock`, publishing into :attr:`events` and
    counting the saves they ask for."""

    def __init__(self, *left: str) -> None:
        self.made = FakeCopies(*left)
        self.clock = Clock()
        self.events: list[Event] = []
        self.saves = 0
        self.copies = RoleCopies(self.made, self.clock, self.publish)
        self.copies.changed = self.save

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    async def save(self) -> None:
        self.saves += 1


def saved(path: str, at: datetime = START) -> SavedCopy:
    return SavedCopy(path=path, last_turn=at)


async def test_a_roles_first_turn_makes_its_copy_and_later_turns_reuse_it() -> None:
    ledger = Ledger()
    first = await ledger.copies.enter("experimenter")
    ledger.clock.move(1)
    assert await ledger.copies.enter("experimenter") == first == "/copies/copy-1"
    assert ledger.made.made == [first]
    assert ledger.copies.held == {"experimenter": saved(first, START + timedelta(hours=1))}


async def test_a_recorded_copy_left_by_an_earlier_run_is_reused() -> None:
    ledger = Ledger("/old/copy")
    ledger.copies.replace({"experimenter": saved("/old/copy")})
    assert await ledger.copies.enter("experimenter") == "/old/copy"
    assert ledger.made.made == []


async def test_a_recorded_copy_thats_gone_is_made_again() -> None:
    ledger = Ledger()
    ledger.copies.replace({"experimenter": saved("/old/copy")})
    assert await ledger.copies.enter("experimenter") == "/copies/copy-1"


async def test_a_copy_that_cant_be_listed_is_made_again_saying_why() -> None:
    ledger = Ledger("/old/copy")
    ledger.made.failures["leftovers"] = TreeFailed("unreadable")
    ledger.copies.replace({"experimenter": saved("/old/copy")})
    assert await ledger.copies.enter("experimenter") == "/copies/copy-1"
    message = "Couldn't list the copies: unreadable"
    assert ledger.events == [ErrorOccurred(code=GIT_FAILED, message=message)]


async def test_dropping_a_roles_copy_removes_it_and_the_next_turn_makes_another() -> None:
    ledger = Ledger()
    first = await ledger.copies.enter("experimenter")
    await ledger.copies.drop("experimenter")
    assert (ledger.made.removed, ledger.copies.held) == ([first], {})
    assert await ledger.copies.enter("experimenter") == "/copies/copy-2"


async def test_dropping_a_role_without_a_copy_does_nothing() -> None:
    ledger = Ledger()
    await ledger.copies.drop("experimenter")
    assert ledger.made.removed == []


async def test_a_copy_that_cant_be_removed_is_left_over_saying_why() -> None:
    ledger = Ledger()
    path = await ledger.copies.enter("experimenter")
    ledger.made.failures["remove"] = TreeFailed("busy")
    await ledger.copies.drop("experimenter")
    message = f"The copy at {path} couldn't be removed and is left over: busy"
    assert ledger.events == [ErrorOccurred(code=GIT_FAILED, message=message)]


async def test_copies_past_their_roles_ttl_are_removed_and_the_session_saved() -> None:
    ledger = Ledger()
    old = await ledger.copies.enter("experimenter")
    ledger.copies.leave("experimenter")
    ledger.clock.move(2)
    young = await ledger.copies.enter("architect")
    ledger.copies.leave("architect")
    ledger.clock.move(1)
    await ledger.copies.expire({"experimenter": 2, "architect": 2})
    assert ledger.made.removed == [old]
    assert list(ledger.copies.held) == ["architect"]
    assert ledger.saves == 1
    assert young not in ledger.made.removed


async def test_a_copy_isnt_expired_while_its_role_takes_a_turn() -> None:
    ledger = Ledger()
    path = await ledger.copies.enter("experimenter")
    ledger.clock.move(3)
    await ledger.copies.expire({"experimenter": 2})
    assert ledger.made.removed == []
    assert ledger.copies.held == {"experimenter": saved(path)}


async def test_a_copys_ttl_counts_from_the_end_of_its_roles_last_turn() -> None:
    ledger = Ledger()
    path = await ledger.copies.enter("experimenter")
    ledger.clock.move(3)
    ledger.copies.leave("experimenter")
    ended = START + timedelta(hours=3)
    assert ledger.copies.held == {"experimenter": saved(path, ended)}
    ledger.clock.move(1)
    await ledger.copies.expire({"experimenter": 2})
    assert (ledger.made.removed, ledger.saves) == ([], 0)
    ledger.clock.move(2)
    await ledger.copies.expire({"experimenter": 2})
    assert ledger.made.removed == [path]


async def test_leaving_a_turn_whose_copy_was_dropped_records_nothing() -> None:
    ledger = Ledger()
    await ledger.copies.enter("experimenter")
    await ledger.copies.drop("experimenter")
    ledger.copies.leave("experimenter")
    assert ledger.copies.held == {}


async def test_no_copy_past_its_ttl_saves_nothing() -> None:
    ledger = Ledger()
    await ledger.copies.enter("experimenter")
    await ledger.copies.expire({"experimenter": 2})
    assert (ledger.made.removed, ledger.saves) == ([], 0)


async def test_a_sweep_removes_expired_recorded_copies_and_unrecorded_leftovers() -> None:
    ledger = Ledger("/mine", "/theirs-old", "/theirs-new", "/stray")
    old = START - timedelta(hours=30)
    ledger.copies.replace({"experimenter": saved("/mine")})
    others = OtherCopies(
        ({"experimenter": saved("/theirs-old", old)}, {"architect": saved("/theirs-new")})
    )
    await ledger.copies.sweep(others, {})
    assert ledger.made.removed == ["/theirs-old", "/stray"]
    assert ledger.copies.held == {"experimenter": saved("/mine")}


async def test_a_sweep_skips_expired_recorded_copies_already_gone() -> None:
    ledger = Ledger()
    others = OtherCopies(({"experimenter": saved("/gone", START - timedelta(hours=30))},))
    await ledger.copies.sweep(others, {})
    await ledger.copies.sweep(others, {})
    assert (ledger.made.removed, ledger.events) == ([], [])


async def test_a_sweep_removes_an_expired_copy_made_this_run_for_a_session_left() -> None:
    ledger = Ledger()
    path = await ledger.copies.enter("experimenter")
    left = ledger.copies.held
    ledger.copies.replace({})
    ledger.clock.move(30)
    await ledger.copies.sweep(OtherCopies((left,)), {})
    assert ledger.made.removed == [path]


async def test_a_sweep_says_why_an_expired_recorded_copy_cant_be_removed() -> None:
    ledger = Ledger("/theirs")
    ledger.made.failures["remove"] = TreeFailed("busy")
    others = OtherCopies(({"experimenter": saved("/theirs", START - timedelta(hours=30))},))
    await ledger.copies.sweep(others, {})
    message = "The copy at /theirs couldn't be removed and is left over: busy"
    assert ledger.events == [ErrorOccurred(code=GIT_FAILED, message=message)]


async def test_keeping_sweeps_at_once_then_expires_the_sessions_copies_every_period() -> None:
    ledger = Ledger("/stray")
    path = await ledger.copies.enter("experimenter")
    ledger.copies.leave("experimenter")
    swept = asyncio.Event()

    async def others() -> OtherCopies:
        swept.set()
        return OtherCopies()

    ttls: Mapping[str, float] = {"experimenter": 1}
    keeping = asyncio.create_task(ledger.copies.keep(others, lambda: ttls, every=0.01))
    await swept.wait()
    ledger.clock.move(2)
    while path not in ledger.made.removed:
        await asyncio.sleep(0.01)
    keeping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await keeping
    assert ledger.made.removed == ["/stray", path]


async def test_a_fresh_copy_is_removed_after_its_body_however_it_ends() -> None:
    made, events = FakeCopies(), list[Event]()

    async def publish(event: Event) -> None:
        events.append(event)

    with pytest.raises(RuntimeError):
        async with fresh_copy(made, publish) as path:
            assert made.removed == []
            raise RuntimeError
    assert made.removed == [path] == ["/copies/copy-1"]


async def test_a_trees_copies_are_of_its_base_commit() -> None:
    tree = FakeTree()
    tree.copies_left = ["/work/copy-old"]
    copies = TreeCopies(tree)
    path = await copies.make()
    await copies.remove(path)
    assert (tree.copied, tree.copies_dropped) == (["base"], [path])
    assert await copies.leftovers() == ["/work/copy-old"]


def test_each_roles_ttl_is_its_copy_ttl() -> None:
    assert copy_ttls(ROLES)["coder"] == 24


def record(session_id: str, copies: Recorded) -> SessionRecord:
    return SessionRecord(
        id=session_id, name=session_id, created=START, last_active=START, copies=dict(copies)
    )


async def test_the_other_saved_sessions_copies_are_read_from_the_store() -> None:
    mine = {"experimenter": saved("/mine")}
    theirs = {"experimenter": saved("/theirs")}
    store = FakeStore()
    store.saved += [record("s-1", mine), record("s-2", theirs)]
    store.broken["s-3"] = "broken"
    assert await saved_copies(store, "s-1") == OtherCopies((theirs,), complete=False)


async def test_the_copies_of_readable_sessions_are_all_known() -> None:
    store = FakeStore()
    store.saved += [record("s-1", {}), record("s-2", {"experimenter": saved("/theirs")})]
    assert await saved_copies(store, "s-1") == OtherCopies(({"experimenter": saved("/theirs")},))


async def test_no_store_records_no_copies_completely() -> None:
    assert await saved_copies(None, "s-1") == OtherCopies()


async def test_an_unlistable_store_records_no_copies_incompletely() -> None:
    failing = FakeStore()
    failing.list_failure = ReadFailed("locked database")
    assert await saved_copies(failing, "s-1") == OtherCopies(complete=False)


async def test_a_sweep_keeps_leftovers_when_a_store_cant_be_listed() -> None:
    ledger = Ledger("/theirs")
    failing = FakeStore()
    failing.list_failure = ReadFailed("locked database")
    await ledger.copies.sweep(await saved_copies(failing, "s-1"), {})
    assert ledger.made.removed == []


async def test_a_sweep_keeps_leftovers_but_expires_readable_copies_past_an_unreadable() -> None:
    ledger = Ledger("/unreadables", "/theirs-old")
    store = FakeStore()
    old = START - timedelta(hours=30)
    store.saved.append(record("s-2", {"experimenter": saved("/theirs-old", old)}))
    store.broken["s-3"] = "broken"
    await ledger.copies.sweep(await saved_copies(store, "s-1"), {"experimenter": 24})
    assert ledger.made.removed == ["/theirs-old"]
