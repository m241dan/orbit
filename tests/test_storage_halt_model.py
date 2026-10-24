"""Following a session halted because storage failed, without the screens showing it: the
status line, the modal, one at a time, and trying again."""

from orbit.protocol import STORAGE_FAILED, CommandAccepted, ResumeStorage
from orbit.tui.view.storage import StorageHalt
from tests.desk_fakes import Backend

EXPLANATION = (
    "Orbit stopped this session: saving the event log to /s/orbit.db failed.\n"
    "The error (disk full): database or disk is full"
)
STILL = "Orbit stopped this session: saving the session record to /s/orbit.db failed."
EVENT_LOG_STATUS = "Session stopped: event log failed; see the transcript"
STILL_STATUS = "Session stopped: session record failed; see the transcript"


class View:
    """What the halt shows, as it shows it: the status line, and the text each modal opened
    shows, in the order they opened; a test closes the one open as the user would, by setting
    ``modal_open``. ``closes`` counts the modals closed from the halt."""

    def __init__(self) -> None:
        self.status: str | None = None
        self.modals: list[str] = []
        self.modal_open = False
        self.closes = 0

    def show_status(self, status: str | None) -> None:
        self.status = status

    def open_modal(self, explanation: str) -> None:
        assert not self.modal_open
        self.modals.append(explanation)
        self.modal_open = True

    def show_in_modal(self, explanation: str) -> None:
        assert self.modal_open
        self.modals[-1] = explanation

    def close_modal(self) -> None:
        if self.modal_open:
            self.closes += 1
            self.modal_open = False


def halt() -> tuple[StorageHalt, View, Backend]:
    """A halt following no halt yet, shown in a view, trying again through a backend."""
    view, backend = View(), Backend()
    return StorageHalt(view, backend.desk.send), view, backend


def test_a_halt_opens_the_modal_with_its_explanation_and_shows_the_status_line() -> None:
    storage, view, backend = halt()
    storage.halted(EXPLANATION)
    assert (view.modals, view.modal_open) == ([EXPLANATION], True)
    assert view.status == EVENT_LOG_STATUS
    assert storage.explanation == EXPLANATION
    assert backend.commands == []


def test_the_modal_closed_by_the_user_leaves_the_status_line_and_the_halt() -> None:
    storage, view, _ = halt()
    storage.halted(EXPLANATION)
    view.modal_open = False
    assert view.status == EVENT_LOG_STATUS
    assert storage.explanation == EXPLANATION
    assert view.closes == 0


def test_a_halt_once_the_user_closed_the_modal_opens_another_with_its_text() -> None:
    storage, view, _ = halt()
    storage.halted(EXPLANATION)
    view.modal_open = False
    storage.halted(STILL)
    assert (view.modals, view.modal_open) == ([EXPLANATION, STILL], True)
    assert view.status == STILL_STATUS


def test_only_one_modal_opens_at_a_time() -> None:
    storage, view, _ = halt()
    storage.halted(EXPLANATION)
    storage.halted(STILL)
    assert view.modals == [STILL]
    assert view.status == STILL_STATUS


async def test_trying_again_sends_resume_storage_and_its_acceptance_lifts_the_halt() -> None:
    storage, view, backend = halt()
    storage.halted(EXPLANATION)
    await storage.retry()
    assert backend.commands == [ResumeStorage()]
    assert view.modal_open
    backend.accept()
    assert (view.modal_open, view.closes) == (False, 1)
    assert view.status is None
    assert storage.explanation is None


async def test_another_commands_acceptance_keeps_the_halt() -> None:
    storage, view, backend = halt()
    storage.halted(EXPLANATION)
    await storage.retry()
    backend.desk.answer(CommandAccepted(command_id="other"))
    assert view.modal_open and view.status == EVENT_LOG_STATUS
    assert storage.explanation == EXPLANATION


async def test_an_earlier_try_agains_acceptance_keeps_the_halt() -> None:
    storage, view, backend = halt()
    storage.halted(EXPLANATION)
    await storage.retry()
    await storage.retry()
    assert backend.commands == [ResumeStorage(), ResumeStorage()]
    backend.accept(0)
    assert view.modal_open and view.status == EVENT_LOG_STATUS


async def test_a_refused_try_again_keeps_the_modal_which_shows_the_halt_it_reports() -> None:
    storage, view, backend = halt()
    storage.halted(EXPLANATION)
    await storage.retry()
    refusal = backend.refuse(STILL, code=STORAGE_FAILED)
    assert view.modal_open and storage.explanation == EXPLANATION
    assert backend.shown == [refusal]
    storage.halted(STILL)  # As the refusal, a storage failure, reports.
    assert view.modals == [STILL]
    assert view.status == STILL_STATUS


def test_lifting_closes_the_modal_and_hides_the_status_line() -> None:
    storage, view, _ = halt()
    storage.halted(EXPLANATION)
    storage.lifted()
    assert (view.modal_open, view.closes) == (False, 1)
    assert view.status is None
    assert storage.explanation is None


def test_lifting_once_the_user_closed_the_modal_hides_the_status_line() -> None:
    storage, view, _ = halt()
    storage.halted(EXPLANATION)
    view.modal_open = False
    storage.lifted()
    assert view.status is None and storage.explanation is None
    assert view.closes == 0


def test_without_a_halt_lifting_opens_and_shows_nothing() -> None:
    storage, view, _ = halt()
    storage.lifted()
    assert (view.modals, view.modal_open, view.closes) == ([], False, 0)
    assert view.status is None and storage.explanation is None


async def test_a_halt_after_the_try_agains_acceptance_opens_a_new_modal() -> None:
    storage, view, backend = halt()
    storage.halted(EXPLANATION)
    await storage.retry()
    backend.accept()
    storage.halted(STILL)
    assert (view.modals, view.modal_open) == ([EXPLANATION, STILL], True)
    assert view.status == STILL_STATUS


def test_a_halt_after_one_lifted_opens_a_new_modal() -> None:
    storage, view, _ = halt()
    storage.halted(EXPLANATION)
    storage.lifted()
    storage.halted(EXPLANATION)
    assert (view.modals, view.modal_open) == ([EXPLANATION, EXPLANATION], True)


def test_a_halt_shows_in_the_modal_opened_since_one_was_closed() -> None:
    storage, view, _ = halt()
    storage.halted(EXPLANATION)
    storage.lifted()
    storage.halted(EXPLANATION)
    storage.halted(STILL)
    assert (view.modals, view.modal_open) == ([EXPLANATION, STILL], True)
