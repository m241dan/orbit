"""The explanation of a session halted because a write to the store failed."""

import pytest

from orbit.agent.domain.storage_halt import (
    NEITHER_PLACE,
    RESUME,
    FailedWrite,
    StorageErrorKind,
    StorageFault,
    halting_message,
    seq_span,
    storage_halt_explanation,
)

DATABASE = "/state/orbit/orbit.db"
FALLBACK = "/state/orbit/fallback/w1-s1.jsonl"


def fault(
    kind: StorageErrorKind = StorageErrorKind.DISK_FULL,
    write: FailedWrite = FailedWrite.EVENT_LOG,
) -> StorageFault:
    return StorageFault(write, DATABASE, "database or disk is full", kind)


def test_the_explanation_reports_the_write_the_database_and_the_error() -> None:
    text = storage_halt_explanation(fault(), (12, 15), FALLBACK)

    assert f"saving the event log to {DATABASE} failed" in text
    assert "The error (disk full): database or disk is full" in text


def test_the_explanation_names_the_failed_session_record() -> None:
    text = storage_halt_explanation(fault(write=FailedWrite.SESSION_RECORD), None, FALLBACK)

    assert "saving the session record to" in text


def test_the_explanation_names_the_unsaved_events_and_the_file_they_went_to() -> None:
    text = storage_halt_explanation(fault(), (12, 15), FALLBACK)

    assert f"The unsaved events, seq 12 to 15, were written to {FALLBACK}" in text


def test_unsaved_events_written_nowhere_say_neither_place_worked() -> None:
    text = storage_halt_explanation(fault(), (12, 15), None)

    assert f"seq 12 to 15, weren't written anywhere: {NEITHER_PLACE}." in text


def test_with_nothing_unsaved_the_explanation_names_where_later_events_go() -> None:
    text = storage_halt_explanation(fault(), None, FALLBACK)

    assert f"No events were left unsaved; later events are written to {FALLBACK}." in text


def test_with_nothing_unsaved_and_nowhere_to_write_it_says_so() -> None:
    text = storage_halt_explanation(fault(), None, None)

    assert f"No events were left unsaved, but {NEITHER_PLACE}" in text


def test_the_explanation_says_saved_data_is_intact_and_how_to_resume() -> None:
    text = storage_halt_explanation(fault(), (1, 1), FALLBACK)

    assert "What was saved is intact, up to the last saved event." in text
    assert text.endswith(RESUME) and '"Try again"' in RESUME and "restart" in RESUME


@pytest.mark.parametrize(
    ("kind", "fix"),
    [
        (StorageErrorKind.DISK_FULL, f"free space on the disk holding {DATABASE}"),
        (StorageErrorKind.READ_ONLY, "check the owner and mode of the state directory"),
        (StorageErrorKind.LOCKED, "another process holds the database"),
        (StorageErrorKind.MISSING, "the state directory holding"),
    ],
)
def test_each_error_kind_gets_its_fix(kind: StorageErrorKind, fix: str) -> None:
    text = storage_halt_explanation(fault(kind), None, FALLBACK)

    assert fix in text and "doesn't recognise" not in text


def test_a_missing_file_says_the_state_directory_was_moved_or_removed() -> None:
    text = storage_halt_explanation(fault(StorageErrorKind.MISSING), None, FALLBACK)

    assert "was moved or removed" in text


def test_an_unknown_error_says_orbit_does_not_recognise_it() -> None:
    text = storage_halt_explanation(fault(StorageErrorKind.UNKNOWN), None, FALLBACK)

    assert "Orbit doesn't recognise this error" in text
    assert "The error (not recognised): database or disk is full" in text


def test_the_span_of_seqs_is_their_first_and_last() -> None:
    assert seq_span([7, 3, 5]) == (3, 7)
    assert seq_span([]) is None


def test_the_halting_message_names_the_failed_write() -> None:
    assert halting_message(FailedWrite.REVIEW_COUNT) == (
        "Saving the review count failed, so orbit is stopping the session."
    )
