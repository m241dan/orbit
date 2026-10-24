"""The picker of a workspace's saved sessions, without the screen showing it: the rows it lists,
what picking sends, naming and renaming in its box, deleting, refusals and its highlight."""

from collections.abc import Awaitable, Callable, Sequence

from orbit.protocol import (
    Attach,
    DeleteSession,
    NewSession,
    RenameSession,
    SessionSummary,
)
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.session_picker import SessionChange, SessionChoice, SessionPicker, StartupPick
from orbit.tui.view.sessions import Viewing
from orbit.tui.view.typing_list import Label
from tests.desk_fakes import Backend
from tests.session_apps import BROKEN, CURRENT, LOCKED, OLD, SWITCH_TO_OLD

OLD_ROW = "  auth rework · Oct 16 18:02"
SAVED_ROWS = [OLD_ROW, "  elsewhere · open in another orbit", "  bad.json · unreadable: not JSON"]
SAVED = (CURRENT, OLD, LOCKED, BROKEN)
ON_W = Viewing("w", "now")
UNJOINED_W = Viewing("w", "now", joined=False)
RUNNING_NOW = CURRENT.model_copy(update={"state": "running"})
CANT_DELETE = "Can't delete today: it's the active session."

Confirmed = tuple[str, str, str, Callable[[], Awaitable[object]]]


class View:
    """What the session picker shows, as it shows it, and what it asks: the highlight is set as
    a test moves it."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend
        self.is_attached = True
        self.is_mounted = True
        self.highlighted: int | None = 0
        self.keys: list[str | None] = []
        self.rows: list[str] = []
        self.hint = ""
        self.note: str | None = None
        self.box: str | None = None  # The text box's text, while it's shown.
        self.box_note = ""
        self.dismissed: list[tuple[SessionChoice, ...]] = []
        self.quit_count = 0
        self.confirmed: list[Confirmed] = []

    def show_rows(
        self, keys: Sequence[str | None], labels: Sequence[Label], highlight: int | None
    ) -> None:
        self.keys, self.rows = list(keys), [str(label) for label in labels]
        self.highlighted = highlight

    def show_hint(self, hint: str) -> None:
        self.hint = hint

    def show_note(self, note: str | None) -> None:
        self.note = note

    def show_box(self, shown: bool, text: str) -> None:
        self.box, self.box_note = (text if shown else None), ""

    def show_box_note(self, note: str) -> None:
        self.box_note = note

    def dismiss(self, result: tuple[SessionChoice, ...]) -> None:
        self.dismissed.append(result)

    def quit(self) -> None:
        self.quit_count += 1

    def confirm(
        self, title: str, message: str, yes: str, then: Callable[[], Awaitable[object]]
    ) -> None:
        self.confirmed.append((title, message, yes, then))

    async def manage(self, change: SessionChange, awaited: Awaited) -> object:
        return await self._backend.desk.send(change, awaited)


def picker(
    sessions: Sequence[SessionSummary] = SAVED,
    viewing: Viewing = ON_W,
    startup: StartupPick | None = "pick",
) -> tuple[SessionPicker, View, Backend]:
    """A picker of ``sessions``, viewing as ``viewing`` says, opened at ``startup``."""
    backend = Backend()
    view = View(backend)
    return SessionPicker(view, sessions, viewing, startup), view, backend


def labels(picked: SessionPicker) -> list[str]:
    return [str(label) for label in picked.labels]


async def renamed(picked: SessionPicker, name: str) -> None:
    """Rename the highlighted session ``name``, as typed in the box."""
    picked.rename()
    await picked.named(name)


def test_a_startup_pick_lists_the_saved_sessions_then_new_session() -> None:
    picked, _, _ = picker()
    assert picked.startup and not picked.resume
    assert labels(picked) == [*SAVED_ROWS, "New session"]
    assert picked.keys == ("old", "busy", "bad", None)


def test_an_unjoined_startup_picker_offers_to_resume_the_active_session_first() -> None:
    picked, view, _ = picker(viewing=UNJOINED_W, startup="resume")
    assert labels(picked) == [
        "  Resume today · Oct 16 18:02 · active · viewed by 1",
        *SAVED_ROWS,
        "New session",
    ]
    picked.choose(0)
    assert view.dismissed == [(Attach(workspace="w", session="now"),)]


def test_with_no_saved_sessions_the_picker_offers_only_a_new_session() -> None:
    picked, _, _ = picker((CURRENT,))
    assert labels(picked) == ["New session"]


def test_picking_with_the_active_session_idle_and_unviewed_activates_it() -> None:
    picked, view, _ = picker()
    picked.choose(0)
    assert view.dismissed == [tuple(SWITCH_TO_OLD)]


def test_picking_while_a_task_runs_only_views_the_session() -> None:
    picked, view, _ = picker((RUNNING_NOW, OLD), viewing=UNJOINED_W, startup="resume")
    assert labels(picked)[0] == "  Resume today · Oct 16 18:02 · active · running · viewed by 1"
    picked.choose(1)
    assert view.dismissed == [(Attach(workspace="w", session="old"),)]


def test_picking_while_another_frontend_views_the_active_session_only_views() -> None:
    viewed = CURRENT.model_copy(update={"viewers": 2})
    picked, view, _ = picker((viewed, OLD), viewing=UNJOINED_W, startup="resume")
    assert labels(picked)[0] == "  Resume today · Oct 16 18:02 · active · viewed by 2"
    picked.choose(1)
    assert view.dismissed == [(Attach(workspace="w", session="old"),)]


def test_locked_and_unreadable_sessions_cannot_be_opened() -> None:
    picked, view, _ = picker()
    picked.choose(1)
    picked.choose(2)
    assert view.dismissed == []


async def test_a_new_session_is_started_with_the_name_typed() -> None:
    picked, view, _ = picker()
    picked.choose(3)
    assert view.box == ""
    assert view.hint == "enter start · esc back"
    await picked.named(" jk ")
    assert view.dismissed == [(NewSession(name="jk"),)]


async def test_a_new_session_without_a_name_leaves_it_to_the_backend() -> None:
    picked, view, _ = picker((CURRENT,))
    picked.choose(0)
    await picked.named(" ")
    assert view.dismissed == [(NewSession(name=None),)]


def test_escape_from_the_name_goes_back_to_the_list() -> None:
    picked, view, _ = picker((CURRENT, LOCKED))
    picked.choose(1)
    picked.back()
    assert view.box is None
    assert view.hint == "j/k move · enter open · r rename · x delete · esc quit"
    assert (view.dismissed, view.quit_count) == ([], 0)


def test_escape_on_the_list_at_startup_quits_orbit() -> None:
    picked, view, _ = picker()
    picked.back()
    assert (view.quit_count, view.dismissed) == (1, [])


def test_ctrl_ps_picker_lists_the_session_viewed_first() -> None:
    picked, view, _ = picker(startup=None)
    assert not picked.startup
    assert labels(picked)[:2] == ["● today · Oct 16 18:02 · active", OLD_ROW]
    picked.choose(1)
    assert view.dismissed == [tuple(SWITCH_TO_OLD)]


def test_choosing_the_session_viewed_closes_with_nothing_to_send() -> None:
    picked, view, _ = picker(startup=None)
    picked.choose(0)
    assert view.dismissed == [()]


def test_escape_mid_session_closes_with_nothing_to_send() -> None:
    picked, view, _ = picker(startup=None)
    picked.back()
    assert (view.dismissed, view.quit_count) == ([()], 0)


async def test_r_asks_for_the_highlighted_sessions_new_name_starting_from_its_name() -> None:
    picked, view, backend = picker()
    picked.rename()
    assert view.box == "auth rework"
    assert view.hint == "enter rename · esc back"
    await picked.named("jk")
    assert backend.commands == [RenameSession(session_id="old", name="jk")]
    assert view.box is None
    assert view.dismissed == []
    assert view.hint == "j/k move · enter open · r rename · x delete · esc quit"


async def test_the_session_viewed_can_be_renamed() -> None:
    picked, _, backend = picker(startup=None)
    await renamed(picked, "today!")
    assert backend.commands == [RenameSession(session_id="now", name="today!")]


async def test_an_empty_new_name_keeps_the_old_one() -> None:
    picked, view, backend = picker()
    await renamed(picked, " ")
    assert backend.commands == []
    assert view.box is None


async def test_escape_from_a_new_name_goes_back_to_the_list_without_renaming() -> None:
    picked, view, backend = picker((CURRENT, OLD))
    picked.rename()
    picked.back()
    assert view.box is None
    picked.choose(1)
    await picked.named("")
    assert view.dismissed == [(NewSession(name=None),)]
    assert backend.commands == []


async def test_r_and_x_do_nothing_on_new_session() -> None:
    picked, view, backend = picker((CURRENT,))
    picked.rename()
    await picked.delete()
    assert (view.box, view.confirmed, backend.commands) == (None, [], [])


async def test_x_deletes_the_highlighted_session_once_confirmed() -> None:
    picked, view, backend = picker()
    await picked.delete()
    [(title, message, yes, then)] = view.confirmed
    assert (title, message, yes) == (
        "Delete session",
        'Delete "auth rework"? Its tasks and review state are gone for good.',
        "delete",
    )
    assert backend.commands == []
    await then()
    assert backend.commands == [DeleteSession(session_id="old")]
    assert view.dismissed == []


async def test_deleting_a_session_open_somewhere_is_left_to_the_backend_to_refuse() -> None:
    picked, view, backend = picker(startup=None)
    await picked.delete()
    view.highlighted = 2
    await picked.delete()
    assert backend.commands == [DeleteSession(session_id="now"), DeleteSession(session_id="busy")]
    assert view.confirmed == []


async def test_a_refused_delete_shows_why_under_the_list_and_nowhere_else() -> None:
    picked, view, backend = picker(startup=None)
    await picked.delete()
    backend.refuse(CANT_DELETE)
    assert (view.note, backend.shown) == (CANT_DELETE, [])


async def test_a_delete_that_fails_to_send_shows_why_once_under_the_list() -> None:
    picked, view, backend = picker()
    backend.failure = RuntimeError("the backend is gone")
    await picked.delete()
    await view.confirmed[0][3]()
    assert backend.commands == [DeleteSession(session_id="old")]
    assert view.note == "Couldn't send the command: the backend is gone"
    assert backend.shown == []


async def test_a_refused_rename_shows_why_under_the_list() -> None:
    picked, view, backend = picker()
    await renamed(picked, "jk")
    backend.refuse("Can't rename: it's open elsewhere.")
    assert (view.note, view.box, backend.shown) == ("Can't rename: it's open elsewhere.", None, [])


async def test_a_rename_refused_while_naming_again_shows_why_in_the_name_boxs_hint() -> None:
    picked, view, backend = picker()
    await renamed(picked, "jk")
    picked.rename()
    backend.refuse("Can't rename: it's open elsewhere.")
    assert (view.box_note, view.note) == ("Can't rename: it's open elsewhere.", None)


async def test_an_accepted_change_shows_nothing() -> None:
    picked, view, backend = picker()
    await picked.delete()
    await view.confirmed[0][3]()
    backend.accept()
    assert (view.note, view.box_note, backend.shown) == (None, "", [])


async def test_the_next_change_sent_clears_the_refusal_shown() -> None:
    picked, view, backend = picker(startup=None)
    await picked.delete()
    backend.refuse(CANT_DELETE)
    view.highlighted = 1
    await renamed(picked, "auth rework!")
    assert view.note is None
    assert backend.commands[-1] == RenameSession(session_id="old", name="auth rework!")


async def test_a_refusal_once_the_picker_closed_shows_nowhere() -> None:
    picked, view, backend = picker(startup=None)
    await picked.delete()
    view.is_attached = False
    backend.refuse(CANT_DELETE)
    assert (view.note, backend.shown) == (None, [])


async def test_a_refused_pick_shows_why_under_the_list_until_the_next_change() -> None:
    picked, view, backend = picker()
    picked.refused("Can't switch: tasks are running.")
    assert view.note == "Can't switch: tasks are running."
    await renamed(picked, "auth rework!")
    assert view.note is None
    assert backend.commands[-1] == RenameSession(session_id="old", name="auth rework!")


def test_a_pick_refused_before_the_picker_is_on_screen_shows_once_it_is() -> None:
    picked, view, _ = picker()
    view.is_mounted = False
    view.is_attached = False
    picked.refused("Can't switch: tasks are running.")
    assert view.note == "Can't switch: tasks are running."


def test_a_pick_refused_while_naming_shows_why_in_the_name_boxs_hint() -> None:
    picked, view, _ = picker()
    picked.choose(3)
    picked.refused("Can't switch: tasks are running.")
    assert (view.box_note, view.note) == ("Can't switch: tasks are running.", None)


def test_listing_again_refreshes_the_rows_keeping_the_highlight_within_the_sessions() -> None:
    picked, view, _ = picker()
    view.highlighted = 2
    renamed_old = OLD.model_copy(update={"name": "flags"})
    picked.show((CURRENT, renamed_old), "now")
    assert picked.sessions == (CURRENT, renamed_old)
    assert view.rows == ["  flags · Oct 16 18:02", "New session"]
    assert view.highlighted == 0
    picked.choose(view.highlighted)
    assert view.dismissed == [tuple(SWITCH_TO_OLD)]


NEWER = SessionSummary(id="new", name="fresh", last_active=OLD.last_active)


def test_a_session_added_above_keeps_the_highlighted_session() -> None:
    picked, view, _ = picker()
    picked.show((CURRENT, NEWER, OLD, LOCKED, BROKEN), "now")
    assert view.highlighted == 1
    assert picked.rows[1] == OLD


def test_reordered_rows_keep_the_highlighted_session() -> None:
    picked, view, _ = picker()
    view.highlighted = 2
    picked.show((CURRENT, BROKEN, OLD, LOCKED), "now")
    assert view.highlighted == 0
    assert picked.rows[0].id == BROKEN.id


def test_a_session_added_keeps_new_session_highlighted() -> None:
    picked, view, _ = picker()
    view.highlighted = 3
    picked.show((CURRENT, NEWER, OLD, LOCKED, BROKEN), "now")
    assert view.highlighted == 4


def test_dropping_the_highlighted_session_keeps_the_highlight_in_place() -> None:
    picked, view, _ = picker()
    picked.show((CURRENT, LOCKED, BROKEN), "now")
    assert view.highlighted == 0
    assert picked.rows[0].id == LOCKED.id
    picked.choose(0)
    assert view.dismissed == []


def test_dropping_the_highlighted_last_session_moves_to_the_nearest_one() -> None:
    picked, view, _ = picker()
    view.highlighted = 2
    picked.show((CURRENT, OLD, LOCKED), "now")
    assert picked.rows == (OLD, LOCKED)
    assert view.highlighted == 1


def test_listing_before_the_picker_is_on_screen_only_keeps_the_sessions() -> None:
    picked, view, _ = picker()
    view.is_mounted = False
    picked.show((CURRENT, OLD), "now")
    assert picked.sessions == (CURRENT, OLD)
    assert view.rows == []


def test_new_session_picked_once_the_list_shrank_names_it() -> None:
    picked, view, _ = picker()
    picked.show((CURRENT, OLD), "now")
    picked.choose(1)
    assert view.box == ""
    assert view.dismissed == []
