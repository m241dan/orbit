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
    review_count_json,
)
from orbit.protocol import Settings, TaskSpec, TaskState, Usage

FIXTURES = Path(__file__).parent / "fixtures"

BUMP = (
    "The saved session format changed. Don't edit {fixture}: bump FORMAT_VERSION, add a "
    "_MIGRATIONS entry from the old version, and pin the new one with a fixture of its own "
    "and a test that the old fixture migrates to it."
)

# Never edit this for a format change: it's what orbits at version 1 wrote, and they still
# need to read. A new version gets its own fixture (session-v2.json) and a migration test
# from this one.
SESSION_V1 = FIXTURES / "session-v1.json"

COUNT_CHANGED = (
    "The saved review count format changed. Don't edit REVIEW_COUNT_V1: the counts orbits "
    "have saved would read as 0, so keep reading them and pin the new format separately."
)

# Never edit this for a format change: it's what orbits have saved as a repository's review
# count, and they still need to read.
REVIEW_COUNT_V1 = '{"since_super":7}'


def tasks() -> tuple[TaskState, ...]:
    spec = TaskSpec(
        prompt="Add the parser", settings=Settings(model="sonnet", tool_access="off"), role="writer"
    )
    usage = Usage(
        input_tokens=100,
        output_tokens=20,
        cache_creation_input_tokens=30,
        cache_read_input_tokens=40,
        cost_usd=1.25,
        turns=6,
        duration_ms=7000,
    )
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


def reviews() -> LaneSnapshot:
    batch = BatchSnapshot(
        round=2,
        cap=4,
        tasks=("2", "5"),
        finished=frozenset({"2"}),
        commits=("c3",),
        missing=("2",),
        filling=False,
    )
    return LaneSnapshot(
        planner=PlannerSnapshot(
            paused=True, next_batch=3, batches={"b2": batch}, rounds={"b1": 1, "b2": 2}
        ),
        jobs={
            "3": ReviewJob(commits=("c1", "c2"), round=1, batch="b1", missing=("6",), whole=True)
        },
        batches={"3": "b2"},
    )


def expected() -> SessionRecord:
    return SessionRecord(
        version=1,
        id="s-golden",
        name="Golden session",
        created=datetime(2026, 10, 7, 9, 5, tzinfo=UTC),
        last_active=datetime(2026, 10, 17, 14, 30, tzinfo=timezone(timedelta(hours=2))),
        settings=Settings(model="opus", tool_access="full"),
        planner_conversation="conversation-1",
        tasks=tasks(),
        reviews=reviews(),
    )


def test_a_version_1_session_file_reads_as_its_record() -> None:
    assert parse_record(SESSION_V1.read_text(encoding="utf-8")) == expected()


def test_a_session_record_is_written_in_the_version_1_format() -> None:
    written = json.loads(record_json(expected()))
    golden = json.loads(SESSION_V1.read_text(encoding="utf-8"))
    assert written == golden, BUMP.format(fixture=SESSION_V1.name)


def test_a_version_1_review_count_reads_as_its_count() -> None:
    assert parse_review_count(REVIEW_COUNT_V1) == 7


def test_a_review_count_is_written_in_the_version_1_format() -> None:
    written = json.loads(review_count_json(7))
    assert written == json.loads(REVIEW_COUNT_V1), COUNT_CHANGED
