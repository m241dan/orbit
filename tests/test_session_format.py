"""The saved formats, pinned by golden files: a session record and a review count as an
older orbit wrote them still read the same, and this orbit still writes them so."""

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from orbit.agent.domain.reviews import BatchSnapshot, LaneSnapshot, PlannerSnapshot, ReviewJob
from orbit.agent.domain.runtimes import CLAUDE
from orbit.agent.domain.sessions import (
    SavedConsultation,
    SavedConversation,
    SavedCopy,
    SessionRecord,
    parse_record,
    parse_review_count,
    record_json,
)
from orbit.protocol import Effort, Settings, TaskSpec, TaskState, Usage
from tests.session_builders import review_count_json

FIXTURES = Path(__file__).parent / "fixtures"

BUMP = (
    "The saved session format changed. Don't edit {fixture}: bump FORMAT_VERSION, add a "
    "_MIGRATIONS entry from the old version, and pin the new one with a fixture of its own "
    "and a test that the old fixture migrates to it."
)

# Never edit these for a format change: they're what orbits at versions 1 to 14 wrote, and
# they still need to read. A new version gets its own fixture (session-v15.json) and a
# migration test from the last one.
SESSION_V1 = FIXTURES / "session-v1.json"
SESSION_V2 = FIXTURES / "session-v2.json"
SESSION_V3 = FIXTURES / "session-v3.json"
SESSION_V4 = FIXTURES / "session-v4.json"
SESSION_V5 = FIXTURES / "session-v5.json"
SESSION_V6 = FIXTURES / "session-v6.json"
SESSION_V7 = FIXTURES / "session-v7.json"
SESSION_V8 = FIXTURES / "session-v8.json"
SESSION_V9 = FIXTURES / "session-v9.json"
SESSION_V10 = FIXTURES / "session-v10.json"
SESSION_V11 = FIXTURES / "session-v11.json"
SESSION_V12 = FIXTURES / "session-v12.json"
SESSION_V13 = FIXTURES / "session-v13.json"
SESSION_V14 = FIXTURES / "session-v14.json"

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
        version=14,
        id="s-golden",
        name="Golden session",
        created=datetime(2026, 10, 7, 9, 5, tzinfo=UTC),
        last_active=datetime(2026, 10, 17, 14, 30, tzinfo=timezone(timedelta(hours=2))),
        role_settings={"planner": Settings(model="opus", tool_access="full")},
        conversations={"planner": SavedConversation(runtime=CLAUDE, handle="conversation-1")},
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


def test_a_version_6_session_file_migrates_to_a_planner_told_nothing() -> None:
    assert parse_record(SESSION_V6.read_text(encoding="utf-8")) == suggesting()


NOTE = (
    "Another record took the number of `docs/00024-a.md`, so orbit renamed it `docs/00025-a.md` "
    "in the tasks still to come. Name it `docs/00025-a.md` from now on."
)


def telling() -> SessionRecord:
    """The golden record as version 7 keeps it, with a note its planner is still to hear."""
    return suggesting().model_copy(update={"planner_notes": (NOTE,)})


def test_a_version_7_session_file_migrates_to_settings_without_effort() -> None:
    assert parse_record(SESSION_V7.read_text(encoding="utf-8")) == telling()


def with_effort(state: TaskState, effort: Effort) -> TaskState:
    """``state`` with its task's settings asking for ``effort``."""
    settings = state.task.settings.model_copy(update={"effort": effort})
    return state.model_copy(update={"task": state.task.model_copy(update={"settings": settings})})


def straining() -> SessionRecord:
    """The golden record as version 8 keeps it, its settings and its first task's with an
    effort."""
    record = telling()
    first, *others = record.tasks
    settings = Settings(model="opus", tool_access="full", effort="high")
    return record.model_copy(
        update={
            "role_settings": {"planner": settings},
            "tasks": (with_effort(first, "low"), *others),
        }
    )


def test_a_version_8_session_file_migrates_to_the_planners_settings_and_conversation() -> None:
    assert parse_record(SESSION_V8.read_text(encoding="utf-8")) == straining()


def test_a_version_8_record_without_a_conversation_migrates_to_none() -> None:
    golden = json.loads(SESSION_V8.read_text(encoding="utf-8"))
    del golden["settings"]
    golden["planner_conversation"] = None
    record = parse_record(json.dumps(golden))
    assert isinstance(record, SessionRecord)
    assert (record.conversations, record.role_settings) == ({}, {"planner": Settings()})


def conversing() -> SessionRecord:
    """The golden record as version 9 keeps it, with an architect's conversation and settings
    beside the planner's."""
    record = straining()
    architect = Settings(model="sonnet", tool_access="restricted", effort="max")
    conversation = SavedConversation(runtime=CLAUDE, handle="conversation-2")
    return record.model_copy(
        update={
            "role_settings": {**record.role_settings, "architect": architect},
            "conversations": {**record.conversations, "architect": conversation},
        }
    )


def test_a_version_9_session_file_migrates_to_conversations_on_claude() -> None:
    assert parse_record(SESSION_V9.read_text(encoding="utf-8")) == conversing()


def test_a_version_10_session_file_migrates_to_spending_no_credits() -> None:
    assert parse_record(SESSION_V10.read_text(encoding="utf-8")) == conversing()


def crediting() -> SessionRecord:
    """The golden record as version 11 keeps it, with credits in what its first task and the
    session spent."""
    record = conversing()
    first, *others = record.tasks
    credited = first.model_copy(update={"usage": first.usage + Usage(credits=0.5)})
    spent = record.spent + Usage(credits=0.75)
    return record.model_copy(update={"tasks": (credited, *others), "spent": spent})


def test_a_version_11_session_file_migrates_to_having_no_copies() -> None:
    assert parse_record(SESSION_V11.read_text(encoding="utf-8")) == crediting()


def copying() -> SessionRecord:
    """The golden record as version 12 keeps it, with the copy its architect works in."""
    last_turn = datetime(2026, 10, 17, 14, 25, tzinfo=timezone(timedelta(hours=2)))
    copy = SavedCopy(path="/repo/.git/orbit/copies/copy-1", last_turn=last_turn)
    return crediting().model_copy(update={"copies": {"architect": copy}})


def test_a_version_12_session_file_migrates_to_having_no_consultations() -> None:
    assert parse_record(SESSION_V12.read_text(encoding="utf-8")) == copying()


def consulting() -> SessionRecord:
    """The golden record as version 13 keeps it, with a consultation pending and one left."""
    pending = SavedConsultation(number=1, asker="planner", consultant="architect", title="Check X")
    left = pending.model_copy(update={"number": 2, "why": "the session was switched"})
    return copying().model_copy(update={"consultations": (pending, left)})


def test_a_version_13_session_file_migrates_to_having_spent_nothing_by_role() -> None:
    assert parse_record(SESSION_V13.read_text(encoding="utf-8")) == consulting()


def charging() -> SessionRecord:
    """The golden record as version 14 keeps it, with what the planner and the experimenter
    spent, the experimenter's consulted by the planner."""
    planner, experimenter = Usage(cost_usd=0.4), Usage(cost_usd=1.1, credits=0.5)
    return consulting().model_copy(
        update={
            "role_spent": {"planner": planner, "experimenter": experimenter},
            "caused_spent": {"planner": {"experimenter": experimenter}},
        }
    )


def test_a_version_14_session_file_reads_as_its_record() -> None:
    assert parse_record(SESSION_V14.read_text(encoding="utf-8")) == charging()


def test_a_session_record_is_written_in_the_version_14_format() -> None:
    written = json.loads(record_json(charging()))
    golden = json.loads(SESSION_V14.read_text(encoding="utf-8"))
    assert written == golden, BUMP.format(fixture=SESSION_V14.name)


def test_a_version_1_review_count_reads_as_its_count() -> None:
    assert parse_review_count(REVIEW_COUNT_V1) == 7


def test_a_review_count_is_written_in_the_version_1_format() -> None:
    written = json.loads(review_count_json(7))
    assert written == json.loads(REVIEW_COUNT_V1), COUNT_CHANGED
