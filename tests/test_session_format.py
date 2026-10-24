"""The saved formats, pinned by golden files: a session record and a review count as an
older orbit wrote them still read the same, and this orbit still writes them so."""

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from orbit.agent.domain.reviews import BatchSnapshot, LaneSnapshot, PlannerSnapshot, ReviewJob
from orbit.agent.domain.sessions import (
    SessionRecord,
    parse_record,
    parse_review_count,
    record_json,
)
from orbit.protocol import Settings, TaskSpec, TaskState, Usage
from tests.session_builders import review_count_json

FIXTURES = Path(__file__).parent / "fixtures"

BUMP = (
    "The saved session format changed. Don't edit {fixture}: bump FORMAT_VERSION, add a "
    "_MIGRATIONS entry from the old version, and pin the new one with a fixture of its own "
    "and a test that the old fixture migrates to it."
)

# Never edit these for a format change: they're what orbits at versions 1 to 6 wrote, and
# they still need to read. A new version gets its own fixture (session-v7.json) and a
# migration test from the last one.
SESSION_V1 = FIXTURES / "session-v1.json"
SESSION_V2 = FIXTURES / "session-v2.json"
SESSION_V3 = FIXTURES / "session-v3.json"
SESSION_V4 = FIXTURES / "session-v4.json"
SESSION_V5 = FIXTURES / "session-v5.json"
SESSION_V6 = FIXTURES / "session-v6.json"

COUNT_CHANGED = (
    "The saved review count format changed. Don't edit REVIEW_COUNT_V1: the counts orbits "
    "have saved would read as 0, so keep reading them and pin the new format separately."
)

# Never edit this for a format change: it's what orbits have saved as a repository's review
# count, and they still need to read.
REVIEW_COUNT_V1 = '{"since_super":7}'


USAGE = Usage(
    input_tokens=100,
    output_tokens=20,
    cache_creation_input_tokens=30,
    cache_read_input_tokens=40,
    cost_usd=1.25,
    turns=6,
    duration_ms=7000,
)

# What the session spent, its planner and a task since removed included.
SPENT = Usage(
    input_tokens=300,
    output_tokens=50,
    cache_creation_input_tokens=60,
    cache_read_input_tokens=80,
    cost_usd=2.5,
    turns=9,
    duration_ms=12000,
)


def tasks() -> tuple[TaskState, ...]:
    spec = TaskSpec(
        prompt="Add the parser", settings=Settings(model="sonnet", tool_access="off"), role="writer"
    )
    usage = USAGE
    return (
        TaskState(
            id="1",
            task=spec,
            status="completed",
            origin="planner",
            attempt=2,
            max_attempts=3,
            usage=usage,
        ),
        TaskState(
            id="2",
            task=TaskSpec(prompt="Fix the parser"),
            status="gave_up",
            fixes="1",
            origin="review",
            attempt=1,
            max_attempts=2,
            batch="b1",
            round=2,
        ),
        TaskState(
            id="3",
            task=TaskSpec(prompt="Review the whole codebase", role="reviewer"),
            status="unverified",
            origin="super_review",
        ),
        TaskState(id="4", task=TaskSpec(prompt="Document the parser"), status="draft"),
    )


def reviews(
    landed: tuple[str, ...] = (),
    reviewed: tuple[str, ...] = (),
    suggested: frozenset[str] = frozenset(),
) -> LaneSnapshot:
    """The review lane's state, its batch's ``landed`` tasks and its job's ``reviewed`` ones
    as version 5 keeps them, and the titles its planner ``suggested`` as version 6 does."""
    batch = BatchSnapshot(
        round=2,
        cap=4,
        tasks=("2", "5"),
        finished=frozenset({"2"}),
        commits=("c3",),
        missing=("2",),
        landed=landed,
        filling=False,
    )
    job = ReviewJob(
        commits=("c1", "c2"), round=1, batch="b1", missing=("6",), whole=True, reviewed=reviewed
    )
    return LaneSnapshot(
        planner=PlannerSnapshot(
            paused=True, next_batch=3, batches={"b2": batch}, suggested=suggested
        ),
        jobs={"3": job},
        batches={"3": "b2"},
    )


def ended() -> tuple[TaskState, ...]:
    """:func:`tasks` with the reasons they ended, as version 4 keeps them."""
    reasons = {
        "2": {"reason": "Gave up", "reason_detail": "The task contradicts itself."},
        "3": {"reason": "Unit tests failed", "reason_detail": "1 failed, 4 passed"},
    }
    return tuple(state.model_copy(update=reasons.get(state.id, {})) for state in tasks())


def expected(
    spent: Usage = SPENT,
    states: tuple[TaskState, ...] | None = None,
    lane: LaneSnapshot | None = None,
) -> SessionRecord:
    return SessionRecord(
        version=6,
        id="s-golden",
        name="Golden session",
        created=datetime(2026, 10, 7, 9, 5, tzinfo=UTC),
        last_active=datetime(2026, 10, 17, 14, 30, tzinfo=timezone(timedelta(hours=2))),
        settings=Settings(model="opus", tool_access="full"),
        planner_conversation="conversation-1",
        tasks=tasks() if states is None else states,
        reviews=reviews() if lane is None else lane,
        spent=spent,
    )


def test_a_version_1_session_file_migrates_to_having_spent_what_its_tasks_did() -> None:
    assert parse_record(SESSION_V1.read_text(encoding="utf-8")) == expected(spent=USAGE)


def test_a_version_2_session_file_migrates_to_dropping_the_planners_rounds() -> None:
    assert parse_record(SESSION_V2.read_text(encoding="utf-8")) == expected()


def test_a_version_3_session_file_migrates_to_tasks_without_reasons() -> None:
    assert parse_record(SESSION_V3.read_text(encoding="utf-8")) == expected()


def test_a_version_4_session_file_migrates_to_reviews_without_their_tasks() -> None:
    assert parse_record(SESSION_V4.read_text(encoding="utf-8")) == expected(states=ended())


def reviewing() -> SessionRecord:
    """The golden record as version 5 keeps it, its reviews naming their tasks."""
    return expected(states=ended(), lane=reviews(landed=("5",), reviewed=("1", "2")))


def test_a_version_5_session_file_migrates_to_a_planner_without_suggested_titles() -> None:
    assert parse_record(SESSION_V5.read_text(encoding="utf-8")) == reviewing()


def suggesting() -> SessionRecord:
    """The golden record as version 6 keeps it, its planner naming the titles it suggested."""
    lane = reviews(landed=("5",), reviewed=("1", "2"), suggested=frozenset({"fix the parser"}))
    return expected(states=ended(), lane=lane)


def test_a_version_6_session_file_reads_as_its_record() -> None:
    assert parse_record(SESSION_V6.read_text(encoding="utf-8")) == suggesting()


def test_a_session_record_is_written_in_the_version_6_format() -> None:
    written = json.loads(record_json(suggesting()))
    golden = json.loads(SESSION_V6.read_text(encoding="utf-8"))
    assert written == golden, BUMP.format(fixture=SESSION_V6.name)


def test_a_version_1_review_count_reads_as_its_count() -> None:
    assert parse_review_count(REVIEW_COUNT_V1) == 7


def test_a_review_count_is_written_in_the_version_1_format() -> None:
    written = json.loads(review_count_json(7))
    assert written == json.loads(REVIEW_COUNT_V1), COUNT_CHANGED
