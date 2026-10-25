"""Saved session records: their default names, JSON form, unreadable files and summaries."""

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from orbit.agent.domain import sessions
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.reviews import BatchSnapshot, LaneSnapshot, PlannerSnapshot, ReviewJob
from orbit.agent.domain.sessions import (
    EMPTY_NAME,
    FORMAT_VERSION,
    SessionRecord,
    Unreadable,
    check_addressed_session,
    default_name,
    deletion_refusal,
    imported_note,
    newest_first,
    openable,
    parse_record,
    parse_review_count,
    record_json,
    record_name,
    renamable,
    resumed_notes,
    resumed_settings,
    session_name,
    stored_summary,
    summarize,
    tasks_summary,
)
from orbit.protocol import (
    INVALID,
    LOCKED,
    WRONG_SESSION,
    SessionSummary,
    Settings,
    TaskSpec,
    TaskState,
    TaskStatus,
    Usage,
)
from tests.session_builders import review_count_json

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
        role_settings={"planner": Settings(model="opus", tool_access="full")},
        conversations={"planner": "conversation-1"},
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
    assert record().version == FORMAT_VERSION == 9


def test_a_record_round_trips_through_json() -> None:
    reviews = LaneSnapshot(
        planner=PlannerSnapshot(
            next_batch=2,
            batches={
                "b1": BatchSnapshot(
                    round=1, cap=5, tasks=("3",), finished=frozenset({"3"}), landed=("3",)
                )
            },
            suggested=frozenset({"add the parser", "fix the parser"}),
        ),
        jobs={
            "4": ReviewJob(
                commits=("c1", "c2"), round=1, batch="b1", missing=("2",), reviewed=("1",)
            )
        },
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


def rename_title(record: dict[str, Any]) -> dict[str, Any]:
    migrated = {key: value for key, value in record.items() if key != "title"}
    return migrated | {"name": record["title"]}


def test_an_older_record_is_migrated_and_stamped_with_the_new_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions, "FORMAT_VERSION", 10)
    monkeypatch.setitem(sessions._MIGRATIONS, 9, rename_title)
    data = {key: value for key, value in fields().items() if key != "name"}
    old = data | {"version": 9, "title": "Old name"}
    result = parse_record(json.dumps(old))
    assert isinstance(result, SessionRecord)
    assert (result.version, result.name) == (10, "Old name")


def version_1(tasks: tuple[TaskState, ...]) -> dict[str, Any]:
    """The fields of a version 1 record holding ``tasks``: it had no spent total."""
    data = json.loads(record_json(record(tasks)))
    assert isinstance(data, dict)
    del data["spent"]
    return data | {"version": 1}


def test_a_version_1_record_has_spent_what_its_tasks_did() -> None:
    tasks = (task("1", usage=Usage(input_tokens=10, cost_usd=0.5)), task("2", usage=Usage(turns=2)))
    result = parse_record(json.dumps(version_1(tasks)))
    assert isinstance(result, SessionRecord)
    assert result.spent == Usage(input_tokens=10, cost_usd=0.5, turns=2)


def test_a_version_1_record_without_tasks_has_spent_nothing() -> None:
    data = version_1(())
    del data["tasks"]
    result = parse_record(json.dumps(data))
    assert isinstance(result, SessionRecord)
    assert result.spent == Usage()


def test_a_version_1_record_with_an_invalid_task_is_unreadable_for_that_task() -> None:
    data = version_1(()) | {"tasks": [{"id": "1"}]}
    assert parse_record(json.dumps(data)) == Unreadable("tasks.0.task: Field required")


def test_the_version_1_migration_sums_usages_of_tasks_with_fields_it_doesnt_know() -> None:
    usages = (Usage(input_tokens=10, cost_usd=0.5), Usage(turns=2))
    tasks = [{"id": str(n), "renamed": True, "usage": u.model_dump()} for n, u in enumerate(usages)]
    migrated = sessions._MIGRATIONS[1]({"tasks": tasks})
    assert Usage.model_validate(migrated["spent"]) == Usage(input_tokens=10, cost_usd=0.5, turns=2)


@pytest.mark.parametrize("tasks", [{"0": {}}, [3], [{"usage": {"turns": -1}}]])
def test_a_version_1_record_with_unreadable_tasks_is_left_for_validation(tasks: object) -> None:
    data = version_1(()) | {"tasks": tasks}
    assert "spent" not in sessions._MIGRATIONS[1](data)
    assert isinstance(parse_record(json.dumps(data)), Unreadable)


def version_2(reviews: object) -> dict[str, object]:
    """The fields of a version 2 record with ``reviews`` as saved."""
    return fields() | {"version": 2, "reviews": reviews}


def test_a_version_2_records_planner_rounds_are_dropped() -> None:
    planner = {"next_batch": 2, "batches": {"b1": {"round": 1, "cap": 5}}, "rounds": {"b1": 1}}
    result = parse_record(json.dumps(version_2({"planner": planner})))
    assert isinstance(result, SessionRecord)
    assert result.reviews == LaneSnapshot(
        planner=PlannerSnapshot(next_batch=2, batches={"b1": BatchSnapshot(round=1, cap=5)})
    )


def test_a_version_2_record_without_reviews_reads_without_them() -> None:
    result = parse_record(json.dumps(version_2(None)))
    assert isinstance(result, SessionRecord)
    assert result.reviews is None


def test_a_version_2_record_with_an_invalid_planner_is_unreadable_for_it() -> None:
    result = parse_record(json.dumps(version_2({"planner": 3})))
    assert isinstance(result, Unreadable)
    assert result.reason.startswith("reviews.planner: ")


def test_a_version_3_records_tasks_read_without_reasons() -> None:
    data = fields() | {"version": 3, "tasks": [{"id": "1", "task": {"prompt": "x"}}]}
    result = parse_record(json.dumps(data))
    assert isinstance(result, SessionRecord)
    assert result.version == FORMAT_VERSION
    assert [(t.reason, t.reason_detail) for t in result.tasks] == [("", "")]


def test_the_version_3_migration_leaves_the_record_as_it_is() -> None:
    data: dict[str, Any] = {"tasks": [{"id": "1"}], "spent": {}}
    assert sessions._MIGRATIONS[3](data) == data


def test_a_version_4_records_reviews_read_without_the_tasks_they_review() -> None:
    batch = {"round": 1, "cap": 5, "tasks": ["2"], "commits": ["c2"]}
    reviews = {
        "planner": {"batches": {"b1": batch}},
        "jobs": {"3": {"commits": ["c1"], "round": 0, "batch": None}},
    }
    data = fields() | {"version": 4, "reviews": reviews}
    result = parse_record(json.dumps(data))
    assert isinstance(result, SessionRecord) and result.reviews is not None
    assert result.version == FORMAT_VERSION
    assert result.reviews.planner.batches["b1"].landed == ()
    assert result.reviews.jobs["3"].reviewed == ()


def test_a_version_5_records_planner_reads_without_the_titles_it_suggested() -> None:
    reviews: dict[str, Any] = {"planner": {"batches": {}}, "jobs": {}}
    data = fields() | {"version": 5, "reviews": reviews}
    result = parse_record(json.dumps(data))
    assert isinstance(result, SessionRecord) and result.reviews is not None
    assert result.version == FORMAT_VERSION
    assert result.reviews.planner.suggested == frozenset()


def test_a_record_keeps_why_its_tasks_ended() -> None:
    ended = task("1", "unverified").model_copy(
        update={"reason": "Unit tests failed", "reason_detail": "1 failed"}
    )
    saved = record(tasks=(ended,))
    assert parse_record(record_json(saved)) == saved


def test_a_saved_planner_snapshot_has_no_rounds() -> None:
    assert "rounds" not in PlannerSnapshot().model_dump()


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


def test_a_summary_counts_tasks_by_status_and_shows_what_the_session_spent() -> None:
    tasks = (
        task("1", "completed", Usage(input_tokens=100, cost_usd=0.5, turns=1)),
        task("2", "gave_up", Usage(cache_read_input_tokens=3)),
        task("3"),
    )
    spent = Usage(input_tokens=900, cost_usd=1.4, turns=4)
    saved = record(tasks).model_copy(update={"spent": spent})
    assert summarize(saved, current=True, locked=True) == SessionSummary(
        id="abc123",
        name="Oct 7 09:05",
        last_active=ACTIVE,
        counts={"completed": 1, "gave_up": 1, "pending": 1},
        usage=spent,
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


def test_the_review_count_round_trips_through_json() -> None:
    assert parse_review_count(review_count_json(7)) == 7


def test_an_unreadable_review_count_is_zero() -> None:
    texts = ("", "{oops", "[]", '{"since_super": -1}', '{"since_super": "many"}')
    assert [parse_review_count(text) for text in texts] == [0] * 5


def test_a_summary_of_tasks_has_no_last_active_time() -> None:
    summary = tasks_summary("new", "Fresh", (task("1", "draft"),), Usage(turns=1))
    assert summary == SessionSummary(
        id="new", name="Fresh", counts={"draft": 1}, usage=Usage(turns=1)
    )


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


def refused(refusal: object) -> tuple[str, str]:
    """The code and message of ``refusal``, which must be a refusal."""
    assert isinstance(refusal, Refused)
    return refusal.code, refusal.message


def test_a_readable_unlocked_session_is_openable() -> None:
    assert openable("abc123", record(), locked=False) == record()


def test_a_locked_session_isnt_openable() -> None:
    refusal = openable("abc123", record(), locked=True)
    message = "Can't open \"Oct 7 09:05\": it's open in another orbit."
    assert refused(refusal) == (LOCKED, message)


def test_an_unreadable_session_isnt_openable() -> None:
    refusal = openable("abc123", Unreadable("it isn't JSON"), locked=False)
    assert refused(refusal) == (INVALID, "Can't open the session abc123: it isn't JSON.")


def test_a_readable_unlocked_session_is_renamable() -> None:
    assert renamable("abc123", record(), locked=False) == record()


def test_a_locked_session_isnt_renamable() -> None:
    refusal = renamable("abc123", record(), locked=True)
    message = "Can't rename \"Oct 7 09:05\": it's open in another orbit."
    assert refused(refusal) == (LOCKED, message)


def test_an_unreadable_session_isnt_renamable() -> None:
    refusal = renamable("abc123", Unreadable("it isn't JSON"), locked=False)
    assert refused(refusal) == (INVALID, "Can't rename the session abc123: it isn't JSON.")


def test_a_session_neither_open_here_nor_elsewhere_can_be_deleted() -> None:
    assert deletion_refusal("abc123", record(), locked=False) is None


def test_an_unreadable_session_can_be_deleted() -> None:
    unreadable = Unreadable("it isn't JSON")
    assert deletion_refusal("abc123", unreadable, locked=False) is None


def test_a_session_open_in_another_orbit_cant_be_deleted() -> None:
    refusal = deletion_refusal("abc123", Unreadable("it isn't JSON"), locked=True)
    assert refused(refusal) == (LOCKED, "Can't delete \"abc123\": it's open in another orbit.")


def test_a_record_goes_by_its_name_and_an_unreadable_one_by_its_id() -> None:
    assert record_name("abc123", record()) == "Oct 7 09:05"
    assert record_name("abc123", Unreadable("it isn't JSON")) == "abc123"


def test_a_session_name_loses_the_spaces_around_it() -> None:
    assert session_name("  auth rework ") == "auth rework"


def test_an_empty_session_name_is_none() -> None:
    assert session_name("  ") is None
    assert session_name(None) is None
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


@pytest.mark.parametrize(
    ("addressed", "viewing"), [(None, None), (None, "b"), ("a", None), ("b", "b")]
)
def test_a_command_for_no_session_or_the_one_viewed_is_addressed_there(
    addressed: str | None, viewing: str | None
) -> None:
    check_addressed_session(addressed, viewing, "a")


def test_a_command_for_another_session_than_the_open_one_viewed_is_refused() -> None:
    with pytest.raises(Refused) as refused:
        check_addressed_session("b", None, "a")
    message = "Session 'b' isn't open; the open session is a."
    assert (refused.value.code, str(refused.value)) == (WRONG_SESSION, message)


def test_a_command_for_another_session_than_the_saved_one_viewed_is_refused() -> None:
    with pytest.raises(Refused) as refused:
        check_addressed_session("a", "b", "a")
    message = "Session 'a' isn't the one viewed; this connection views b."
    assert (refused.value.code, str(refused.value)) == (WRONG_SESSION, message)


def test_the_import_note_says_how_many_sessions_came_from_where() -> None:
    assert imported_note(3, ".git/orbit/sessions", "sessions.imported") == (
        "Imported 3 saved sessions from .git/orbit/sessions; the old folder is now "
        "sessions.imported."
    )


def test_the_import_note_counts_one_session_as_one() -> None:
    assert imported_note(1, ".orbit/sessions", "sessions.imported-2") == (
        "Imported 1 saved session from .orbit/sessions; the old folder is now sessions.imported-2."
    )
