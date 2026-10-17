"""Saved session records: their default names, JSON form, unreadable files and summaries."""

import json
from datetime import UTC, datetime, timedelta, timezone

from orbit.agent.domain.reviews import BatchSnapshot, LaneSnapshot, PlannerSnapshot, ReviewJob
from orbit.agent.domain.sessions import (
    EMPTY_NAME,
    FORMAT_VERSION,
    SessionRecord,
    Unreadable,
    default_name,
    deletion_refusal,
    lock_is_stale,
    lock_owner,
    newest_first,
    openable,
    parse_record,
    parse_review_count,
    record_json,
    record_name,
    renamable,
    resumed_notes,
    resumed_settings,
    review_count_json,
    session_name,
    stored_summary,
    summarize,
    tasks_summary,
)
from orbit.protocol import SessionSummary, Settings, TaskSpec, TaskState, TaskStatus, Usage

CREATED = datetime(2026, 10, 7, 9, 5, tzinfo=UTC)
ACTIVE = datetime(2026, 10, 17, 14, 30, tzinfo=timezone(timedelta(hours=2)))

NO_USAGE = Usage()


def task(task_id: str, status: TaskStatus = "pending", usage: Usage = NO_USAGE) -> TaskState:
    return TaskState(id=task_id, task=TaskSpec(prompt="x"), status=status, usage=usage)


def record(tasks: tuple[TaskState, ...] = (), reviews: LaneSnapshot | None = None) -> SessionRecord:
    return SessionRecord(
        id="abc123",
        name="Oct 7 09:05",
        created=CREATED,
        last_active=ACTIVE,
        settings=Settings(model="opus", tool_access="full"),
        planner_conversation="conversation-1",
        tasks=tasks,
        reviews=reviews,
    )


def fields() -> dict[str, object]:
    loaded = json.loads(record_json(record()))
    assert isinstance(loaded, dict)
    return loaded


def test_the_default_name_is_the_month_day_and_time() -> None:
    assert default_name(datetime(2026, 10, 17, 14, 30, tzinfo=UTC)) == "Oct 17 14:30"


def test_the_default_name_drops_the_days_leading_zero_but_pads_the_time() -> None:
    assert default_name(datetime(2026, 1, 5, 7, 3, tzinfo=UTC)) == "Jan 5 07:03"


def test_a_new_record_carries_the_current_format_version() -> None:
    assert record().version == FORMAT_VERSION == 1


def test_a_record_round_trips_through_json() -> None:
    reviews = LaneSnapshot(
        planner=PlannerSnapshot(
            next_batch=2,
            batches={"b1": BatchSnapshot(round=1, cap=5, tasks=("3",), finished=frozenset({"3"}))},
            rounds={"b1": 1},
        ),
        jobs={"4": ReviewJob(commits=("c1", "c2"), round=1, batch="b1", missing=("2",))},
        batches={"4": "b1"},
    )
    saved = record(
        tasks=(task("1", "completed", Usage(input_tokens=5, cost_usd=0.5)), task("3")),
        reviews=reviews,
    )
    assert parse_record(record_json(saved)) == saved


def test_a_record_without_reviews_round_trips_through_json() -> None:
    assert parse_record(record_json(record())) == record()


def test_text_that_isnt_json_is_unreadable() -> None:
    result = parse_record("{not json")
    assert isinstance(result, Unreadable)
    assert result.reason.startswith("it isn't JSON:")


def test_json_that_isnt_an_object_is_unreadable() -> None:
    assert parse_record("[1, 2]") == Unreadable("it isn't a JSON object")


def test_a_record_without_a_version_is_unreadable() -> None:
    data = fields()
    del data["version"]
    assert parse_record(json.dumps(data)) == Unreadable("its version isn't a whole number: None")


def test_a_record_whose_version_isnt_a_whole_number_is_unreadable() -> None:
    data = fields() | {"version": True}
    assert parse_record(json.dumps(data)) == Unreadable("its version isn't a whole number: True")


def test_a_record_from_a_newer_orbit_is_unreadable() -> None:
    data = fields() | {"version": FORMAT_VERSION + 1}
    assert parse_record(json.dumps(data)) == Unreadable(
        f"it was written by a newer orbit (format {FORMAT_VERSION + 1}; "
        f"this one reads up to {FORMAT_VERSION})"
    )


def test_a_record_from_before_the_first_version_is_unreadable() -> None:
    data = fields() | {"version": 0}
    assert parse_record(json.dumps(data)) == Unreadable("its version is unknown: 0")


def test_a_record_missing_a_field_is_unreadable() -> None:
    data = fields()
    del data["name"]
    assert parse_record(json.dumps(data)) == Unreadable("name: Field required")


def test_a_record_with_an_invalid_field_is_unreadable() -> None:
    data = fields() | {"tasks": [{"id": "1"}]}
    assert parse_record(json.dumps(data)) == Unreadable("tasks.0.task: Field required")


def test_a_record_with_a_naive_time_is_unreadable() -> None:
    data = fields() | {"created": "2026-10-17T14:30:00"}
    result = parse_record(json.dumps(data))
    assert isinstance(result, Unreadable)
    assert result.reason.startswith("created: ")


def test_a_summary_counts_tasks_by_status_and_adds_up_their_usage() -> None:
    tasks = (
        task("1", "completed", Usage(input_tokens=100, cost_usd=0.5, turns=1)),
        task("2", "completed", Usage(output_tokens=20, cost_usd=0.25, turns=2)),
        task("3", "gave_up", Usage(cache_read_input_tokens=3)),
        task("4"),
    )
    assert summarize(record(tasks), current=True, locked=True) == SessionSummary(
        id="abc123",
        name="Oct 7 09:05",
        last_active=ACTIVE,
        counts={"completed": 2, "gave_up": 1, "pending": 1},
        usage=Usage(
            input_tokens=100, output_tokens=20, cache_read_input_tokens=3, cost_usd=0.75, turns=3
        ),
        current=True,
        locked=True,
    )


def test_a_summary_of_a_session_without_tasks_is_empty_and_neither_current_nor_locked() -> None:
    summary = summarize(record())
    assert (summary.counts, summary.usage, summary.current, summary.locked) == (
        {},
        Usage(),
        False,
        False,
    )
    assert summary.unreadable is None


def test_a_lock_names_its_owners_process_id() -> None:
    assert lock_owner("4242\n") == 4242


def test_a_lock_that_isnt_a_positive_process_id_has_no_owner() -> None:
    assert [lock_owner(text) for text in ("", "orbit", "-3", "0", "4.2", "٣")] == [None] * 6


def test_a_lock_whose_process_is_alive_isnt_stale() -> None:
    assert not lock_is_stale("4242", lambda pid: pid == 4242)


def test_a_lock_whose_process_is_dead_is_stale() -> None:
    assert lock_is_stale("4242", lambda pid: False)


def test_a_lock_without_an_owner_is_stale_without_asking_about_a_process() -> None:
    asked: list[int] = []

    def alive(pid: int) -> bool:
        asked.append(pid)
        return True

    assert lock_is_stale("garbage", alive)
    assert asked == []


def test_the_review_count_round_trips_through_json() -> None:
    assert parse_review_count(review_count_json(7)) == 7


def test_an_unreadable_review_count_is_zero() -> None:
    texts = ("", "{oops", "[]", '{"since_super": -1}', '{"since_super": "many"}')
    assert [parse_review_count(text) for text in texts] == [0] * 5


def test_a_summary_of_tasks_has_no_last_active_time() -> None:
    summary = tasks_summary("new", "Fresh", (task("1", "draft"),))
    assert summary == SessionSummary(id="new", name="Fresh", counts={"draft": 1})


def test_a_readable_stored_session_is_summarized_from_its_record() -> None:
    assert stored_summary("abc123", record(), True, False) == summarize(record(), current=True)


def test_an_unreadable_stored_session_goes_by_its_id_and_says_why() -> None:
    summary = stored_summary("broken", Unreadable("it isn't JSON"), False, True)
    assert summary == SessionSummary(
        id="broken", name="broken", locked=True, unreadable="it isn't JSON"
    )


def summary_at(session_id: str, when: datetime | None) -> SessionSummary:
    return SessionSummary(id=session_id, name=session_id, last_active=when)


def test_sessions_list_newest_first_with_undated_ones_last() -> None:
    old = summary_at("old", CREATED)
    new = summary_at("new", ACTIVE)
    undated = summary_at("undated", None)
    assert newest_first([undated, old, new]) == (new, old, undated)


def test_a_readable_unlocked_session_is_openable() -> None:
    assert openable("abc123", record(), locked=False) == record()


def test_a_locked_session_isnt_openable() -> None:
    assert openable("abc123", record(), locked=True) == (
        "Can't open \"Oct 7 09:05\": it's open in another orbit."
    )


def test_an_unreadable_session_isnt_openable() -> None:
    reason = openable("abc123", Unreadable("it isn't JSON"), locked=False)
    assert reason == "Can't open the session abc123: it isn't JSON."


def test_a_readable_unlocked_session_is_renamable() -> None:
    assert renamable("abc123", record(), locked=False) == record()


def test_a_locked_session_isnt_renamable() -> None:
    assert renamable("abc123", record(), locked=True) == (
        "Can't rename \"Oct 7 09:05\": it's open in another orbit."
    )


def test_an_unreadable_session_isnt_renamable() -> None:
    reason = renamable("abc123", Unreadable("it isn't JSON"), locked=False)
    assert reason == "Can't rename the session abc123: it isn't JSON."


def test_a_session_neither_open_here_nor_elsewhere_can_be_deleted() -> None:
    assert deletion_refusal("abc123", record(), locked=False) is None


def test_an_unreadable_session_can_be_deleted() -> None:
    unreadable = Unreadable("it isn't JSON")
    assert deletion_refusal("abc123", unreadable, locked=False) is None


def test_a_session_open_in_another_orbit_cant_be_deleted() -> None:
    refusal = deletion_refusal("abc123", Unreadable("it isn't JSON"), locked=True)
    assert refusal == "Can't delete \"abc123\": it's open in another orbit."


def test_a_record_goes_by_its_name_and_an_unreadable_one_by_its_id() -> None:
    assert record_name("abc123", record()) == "Oct 7 09:05"
    assert record_name("abc123", Unreadable("it isn't JSON")) == "abc123"


def test_a_session_name_loses_the_spaces_around_it() -> None:
    assert session_name("  auth rework ") == "auth rework"


def test_an_empty_session_name_is_none() -> None:
    assert session_name("  ") is None
    assert EMPTY_NAME == "A session needs a name."


def test_resuming_a_session_names_it_and_when_it_was_last_active() -> None:
    assert resumed_notes(record(), 0) == ('Resumed "Oct 7 09:05" (last active Oct 17 14:30)',)


def test_resuming_says_how_many_tasks_were_interrupted() -> None:
    assert resumed_notes(record(), 1)[1:] == ("1 task was interrupted and is cancelled.",)
    assert resumed_notes(record(), 3)[1:] == ("3 tasks were interrupted and are cancelled.",)


def test_resumed_settings_are_the_saved_ones_but_for_those_pinned() -> None:
    saved = Settings(model="opus", tool_access="full")
    given = Settings(model="haiku", tool_access="off")
    assert resumed_settings(saved, given, ()) == saved
    assert resumed_settings(saved, given, ("model",)) == Settings(model="haiku", tool_access="full")
