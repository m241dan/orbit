from orbit.tui.view.storage import HALT_HINT, HALT_TITLE, halt_status

EVENT_LOG = (
    "Orbit stopped this session: saving the event log to /s/orbit.db failed.\n"
    "The error (disk full): database or disk is full"
)


def test_the_status_names_the_failed_event_log() -> None:
    assert halt_status(EVENT_LOG) == "Session stopped: event log failed; see the transcript"


def test_the_status_names_the_failed_session_record() -> None:
    text = "Saving the session record failed, so orbit is stopping the session."
    assert halt_status(text) == "Session stopped: session record failed; see the transcript"


def test_the_status_names_the_failed_review_count() -> None:
    text = "Orbit stopped this session: saving the review count to /s/orbit.db failed."
    assert halt_status(text) == "Session stopped: review count failed; see the transcript"


def test_the_status_reads_only_the_first_line() -> None:
    text = "Something went wrong.\nThe event log is fine."
    assert halt_status(text) == "Session stopped: storage failed; see the transcript"


def test_the_modal_is_titled_and_names_its_keys() -> None:
    assert HALT_TITLE == "Session stopped: storage failed"
    assert HALT_HINT == "enter try again · esc close"
