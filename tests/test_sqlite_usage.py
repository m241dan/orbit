"""The SQLite store as the usage ledger: each finished turn's row, and the totals per workspace
and in all."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from orbit.agent.application.ports import ReadFailed, StorageFailed, UsageLedger
from orbit.agent.domain.usage_totals import SpendTotals, UsageRow
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.agent.external.workspace_registry import WorkspaceRegistry
from orbit.protocol import Usage
from tests.store_views import Opener, OwnView, database, record

TODAY = datetime(2026, 10, 22, tzinfo=timezone(timedelta(hours=2)))


def row(view: OwnView, usage: Usage, at: datetime, session: str = "abc") -> UsageRow:
    """A coder's turn in the session ``session`` of ``view``'s workspace that cost ``usage``,
    finishing ``at``."""
    return UsageRow(view.workspace_id, session, "coder", "opus", usage, at)


async def test_the_sqlite_store_is_a_usage_ledger(open_store: Opener, workspace: Path) -> None:
    ledger: UsageLedger = (await open_store(workspace)).ledger
    assert isinstance(ledger, SqliteStore)


async def test_a_recorded_turn_is_a_row_with_its_time_in_utc(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    view = await open_store(workspace)
    usage = Usage(
        input_tokens=1,
        output_tokens=2,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=4,
        cost_usd=0.5,
        credits=0.25,
    )
    await view.ledger.record(UsageRow(view.workspace_id, "abc", "planner", None, usage, TODAY))
    with database(state) as connection:
        assert connection.execute("SELECT * FROM usage").fetchall() == [
            (
                view.workspace_id,
                "abc",
                "planner",
                None,
                1,
                2,
                3,
                4,
                0.5,
                "2026-10-21T22:00:00.000000+00:00",
                0.25,
            ),
        ]


async def test_today_counts_the_rows_from_its_start_and_lifetime_every_row(
    open_store: Opener, workspace: Path
) -> None:
    view = await open_store(workspace)
    await view.ledger.record(row(view, Usage(input_tokens=1), TODAY - timedelta(microseconds=1)))
    await view.ledger.record(row(view, Usage(input_tokens=10), TODAY))
    await view.ledger.record(
        row(view, Usage(output_tokens=5, cost_usd=0.25), TODAY + timedelta(hours=3))
    )
    await view.ledger.record(row(view, Usage(credits=0.5), TODAY + timedelta(hours=4)))
    totals = await view.ledger.totals(TODAY)
    expected = SpendTotals(
        Usage(input_tokens=10, output_tokens=5, cost_usd=0.25, credits=0.5),
        Usage(input_tokens=11, output_tokens=5, cost_usd=0.25, credits=0.5),
    )
    assert totals.workspaces == {view.workspace_id: expected}
    assert totals.total == expected


async def test_lifetime_adds_the_spend_sessions_recorded_before_their_turns_were(
    open_store: Opener, tmp_path: Path, workspace: Path
) -> None:
    view = await open_store(workspace)
    other = tmp_path / "other"
    other.mkdir()
    registry = WorkspaceRegistry(view.store)
    other_id = (await registry.find_or_register(str(other))).workspace_id
    spent = Usage(input_tokens=100, cost_usd=1.0, turns=4)
    await view.save(record("abc").model_copy(update={"spent": spent}))
    await view.ledger.record(row(view, Usage(input_tokens=30, cost_usd=0.25), TODAY))
    await view.store.workspace_view(other_id).save(
        record("old").model_copy(update={"spent": spent})
    )
    totals = await view.ledger.totals(TODAY)
    assert totals.workspaces == {
        view.workspace_id: SpendTotals(
            Usage(input_tokens=30, cost_usd=0.25), Usage(input_tokens=100, cost_usd=1.0)
        ),
        other_id: SpendTotals(Usage(), Usage(input_tokens=100, cost_usd=1.0)),
    }
    assert totals.total.lifetime == Usage(input_tokens=200, cost_usd=2.0)


async def test_a_session_record_that_cant_be_read_adds_no_earlier_spend(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    view = await open_store(workspace)
    with database(state) as connection:
        connection.execute(
            "INSERT INTO sessions VALUES (?, 'bad', 'bad', '', 'not json')", (view.workspace_id,)
        )
    totals = await view.ledger.totals(TODAY)
    assert (totals.workspaces, totals.total) == ({}, SpendTotals())


async def test_purging_a_workspace_deletes_its_usage(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    view = await open_store(workspace)
    await view.ledger.record(row(view, Usage(input_tokens=1), TODAY))
    await WorkspaceRegistry(view.store).purge(view.workspace_id)
    with database(state) as connection:
        assert connection.execute("SELECT count(*) FROM usage").fetchone() == (0,)


async def test_recording_once_the_store_is_closed_fails_as_storage(
    open_store: Opener, workspace: Path
) -> None:
    view = await open_store(workspace)
    await view.close()
    with pytest.raises(StorageFailed, match="Couldn't record the usage of a turn in the session"):
        await view.ledger.record(row(view, Usage(), datetime.now(UTC)))


async def test_adding_up_once_the_store_is_closed_fails_as_a_read(
    open_store: Opener, workspace: Path
) -> None:
    view = await open_store(workspace)
    await view.close()
    with pytest.raises(ReadFailed, match="Couldn't add up the usage in"):
        await view.ledger.totals(TODAY)
