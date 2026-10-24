"""Read-only views of sessions, without Textual: the mark naming the session viewed, and the offer
to make it the active one, with what it says and offers."""

from orbit.protocol import (
    BUSY,
    ActivateSession,
    Activity,
    Attach,
    CommandRefused,
    SessionRef,
    SessionStateChanged,
    SessionSummary,
    Snapshot,
)
from orbit.tui.view.confirm import Question
from orbit.tui.view.navigation import Navigation
from orbit.tui.view.opening import Opening
from tests.desk_fakes import Backend
from tests.navigation_fakes import Modals, changes, fed, navigating
from tests.session_apps import CATALOG, CURRENT, OLD, RUNNING, SWITCH_TO_OLD, catalog
from tests.tui_fakes import opened

VIEWING_OLD = "viewing auth rework; today is idle (enter to switch)"
TODAY = SessionRef(id="now", name="today")
THIRD = SessionSummary(id="third", name="flags")
LEFT = SessionStateChanged(session="now", active=False, state="idle", viewers=0)
VIEWED = RUNNING.model_copy(update={"state": "idle", "viewers": 2})
"""The active session idle, but viewed by another frontend."""
RUNNING_T3 = RUNNING.model_copy(update={"activity": (Activity(kind="task_run", task_id="t3"),)})
"""The active session running its task ``t3``."""
REVIEWING_T3 = RUNNING.model_copy(update={"activity": (Activity(kind="review", task_id="t3"),)})
"""The active session reviewing its task ``t3``."""
IDLE = RUNNING.model_copy(update={"state": "idle"})
ACTIVATE = ["Make this the active session", "Just look"]
OPENED = Attach(workspace="w", session="now")
"""What a frontend in process attaches to on the first catalog: the active session."""


def view(summary: SessionSummary) -> Snapshot:
    """A read-only view of ``summary``'s session, while ``today`` is the active one."""
    return opened(summary).model_copy(update={"active": False, "open_session": TODAY})


def started() -> tuple[Navigation, Modals, Backend]:
    """A frontend that picks no session at startup, in this process."""
    return navigating()


def joined() -> tuple[Navigation, Modals, Backend]:
    """A frontend linked to a control plane, picking a session at startup once the user picks
    the workspace, which it attaches to only then."""
    return navigating(Opening(pick=True), linked=True)


def offer(modals: Modals) -> Question:
    """The offer shown, which there must be just one of."""
    [(question, _)] = modals.offers
    return question


async def picked_old(navigation: Navigation, modals: Modals, backend: Backend) -> None:
    """Pick the workspace, then auth rework among its sessions."""
    await modals.settle()
    workspaces = modals.workspaces
    assert workspaces is not None
    await workspaces.choose(0)
    await modals.settle()
    sessions = modals.sessions
    assert sessions is not None
    sessions.choose(1)
    await modals.settle()


async def test_a_view_shows_the_viewing_mark_naming_the_session() -> None:
    navigation, modals, backend = started()
    await fed(navigation, modals, backend, view(OLD))
    assert navigation.place.mark == VIEWING_OLD


async def test_a_live_snapshot_removes_the_viewing_mark() -> None:
    navigation, modals, backend = started()
    await fed(navigation, modals, backend, view(OLD), opened(OLD))
    assert navigation.place.mark is None


async def test_the_viewing_mark_follows_a_rename_in_the_catalog() -> None:
    navigation, modals, backend = started()
    renamed = OLD.model_copy(update={"name": "flags"})
    await fed(navigation, modals, backend, view(OLD), catalog(CURRENT, renamed))
    assert navigation.place.mark == "viewing flags; today is idle (enter to switch)"


async def test_picking_a_session_viewed_read_only_offers_to_make_it_active() -> None:
    navigation, modals, backend = joined()
    await fed(navigation, modals, backend, CATALOG, VIEWED)
    await picked_old(navigation, modals, backend)
    await fed(navigation, modals, backend, view(OLD))
    question = offer(modals)
    assert list(question.choices) == ACTIVATE
    assert question.message == (
        "auth rework is a read-only view; today is the active session, which runs work."
    )
    await modals.answer_offer(0)
    assert changes(backend) == SWITCH_TO_OLD
    assert modals.offers == []


async def test_picking_a_session_made_active_while_the_picker_is_open_offers_nothing() -> None:
    navigation, modals, backend = joined()
    await fed(navigation, modals, backend, CATALOG, VIEWED)
    workspaces = modals.workspaces
    assert workspaces is not None
    await workspaces.choose(0)
    await modals.settle()
    made_active = OLD.model_copy(update={"active": True})
    left = CURRENT.model_copy(update={"active": False})
    await fed(navigation, modals, backend, catalog(left, made_active))
    sessions, view_of = modals.sessions, modals.sessions_view
    assert sessions is not None and view_of is not None
    assert view_of.highlighted == 1  # Today, now listed after auth rework.
    sessions.choose(0)
    await modals.settle()
    await fed(navigation, modals, backend, view(OLD))
    assert changes(backend) == [Attach(workspace="w", session="old")]
    assert modals.offers == []


async def test_picking_a_session_no_longer_active_since_the_picker_opened_offers_it() -> None:
    navigation, modals, backend = joined()
    was_active = OLD.model_copy(update={"active": True})
    left = CURRENT.model_copy(update={"active": False})
    await fed(navigation, modals, backend, catalog(left, was_active))
    workspaces = modals.workspaces
    assert workspaces is not None
    await workspaces.choose(0)
    await modals.settle()
    await fed(navigation, modals, backend, CATALOG, VIEWED)
    sessions, view_of = modals.sessions, modals.sessions_view
    assert sessions is not None and view_of is not None
    assert view_of.highlighted == 1  # The highlight stays on auth rework, now listed second.
    sessions.choose(1)
    await modals.settle()
    await fed(navigation, modals, backend, view(OLD))
    assert changes(backend) == [Attach(workspace="w", session="old")]
    assert list(offer(modals).choices) == ACTIVATE


async def test_just_look_or_escape_closes_the_offer_and_keeps_the_view() -> None:
    for answer in (1, None):
        navigation, modals, backend = joined()
        await fed(navigation, modals, backend, CATALOG, VIEWED)
        await picked_old(navigation, modals, backend)
        await fed(navigation, modals, backend, view(OLD))
        await modals.answer_offer(answer)
        assert changes(backend) == [Attach(workspace="w", session="old")]
        assert modals.offers == []
        assert navigation.place.mark == VIEWING_OLD


async def test_escape_answers_the_offer_as_just_looking() -> None:
    navigation, modals, backend = joined()
    await fed(navigation, modals, backend, CATALOG, VIEWED)
    await picked_old(navigation, modals, backend)
    await fed(navigation, modals, backend, view(OLD))
    question = offer(modals)
    assert question.choices[question.escape] == "Just look"


async def test_with_the_active_session_running_the_offer_only_offers_to_just_look() -> None:
    navigation, modals, backend = joined()
    await fed(navigation, modals, backend, CATALOG, RUNNING_T3)
    await picked_old(navigation, modals, backend)
    await fed(navigation, modals, backend, view(OLD))
    question = offer(modals)
    assert list(question.choices) == ["Just look"]
    assert question.message == "today is running a task; you can switch once it's idle."
    await modals.answer_offer(0)
    assert changes(backend) == [Attach(workspace="w", session="old")]


async def test_a_session_activated_directly_offers_nothing() -> None:
    navigation, modals, backend = joined()
    await fed(navigation, modals, backend, CATALOG)
    await picked_old(navigation, modals, backend)
    await fed(navigation, modals, backend, opened(OLD))
    assert modals.offers == []


async def test_a_view_not_picked_here_offers_nothing_until_asked() -> None:
    navigation, modals, backend = started()
    await fed(navigation, modals, backend, CATALOG, view(OLD))
    assert modals.offers == []
    navigation.place.offer()
    assert list(offer(modals).choices) == ACTIVATE
    await modals.answer_offer(0)
    assert changes(backend) == [OPENED, ActivateSession(session="old")]


async def test_asking_for_the_offer_while_live_or_shown_offers_nothing_more() -> None:
    navigation, modals, backend = started()
    await fed(navigation, modals, backend, CATALOG, opened(CURRENT))
    navigation.place.offer()
    assert (modals.offers, changes(backend)) == ([], [OPENED])
    await fed(navigation, modals, backend, view(OLD))
    navigation.place.offer()
    navigation.place.offer()
    assert len(modals.offers) == 1


async def test_a_refusal_of_activating_is_shown_where_refusals_show() -> None:
    navigation, modals, backend = started()
    await fed(navigation, modals, backend, CATALOG, view(OLD))
    navigation.place.offer()
    await modals.answer_offer(0)
    refused = CommandRefused(
        command_id=backend.sent[-1].command_id,
        code=BUSY,
        message="Can't switch sessions while tasks run.",
    )
    await fed(navigation, modals, backend, refused)
    assert backend.shown[-1] == refused


async def offered_after(*events: object) -> Question:
    """The offer asked for in a frontend that followed ``CATALOG``, a view of auth rework and
    ``events``."""
    navigation, modals, backend = started()
    await fed(navigation, modals, backend, *events)  # type: ignore[arg-type]
    navigation.place.offer()
    return offer(modals)


async def test_a_state_change_activating_a_third_session_names_it_in_the_offer() -> None:
    third = SessionStateChanged(session="third", active=True, state="idle", viewers=1)
    question = await offered_after(catalog(CURRENT, OLD, THIRD), view(OLD), LEFT, third)
    assert question.message == (
        "auth rework is a read-only view; flags is the active session, which runs work."
    )


async def test_a_state_change_running_a_third_session_names_it_in_the_offer() -> None:
    third = SessionStateChanged(session="third", active=True, state="running", viewers=1)
    question = await offered_after(catalog(CURRENT, OLD, THIRD), view(OLD), LEFT, third)
    assert question.message == "flags is running work; you can switch once it's idle."
    assert list(question.choices) == ["Just look"]


async def test_a_catalog_activating_a_third_session_names_it_in_the_offer() -> None:
    left = CURRENT.model_copy(update={"active": False})
    third = THIRD.model_copy(update={"active": True})
    question = await offered_after(CATALOG, view(OLD), catalog(left, OLD, third))
    assert question.message == (
        "auth rework is a read-only view; flags is the active session, which runs work."
    )


async def test_the_offer_names_the_session_viewed_as_renamed_in_the_catalog() -> None:
    renamed = OLD.model_copy(update={"name": "auth v2"})
    question = await offered_after(CATALOG, view(OLD), catalog(CURRENT, renamed))
    assert question.message == (
        "auth v2 is a read-only view; today is the active session, which runs work."
    )


async def test_a_live_snapshot_closes_the_offer_shown_and_sends_nothing() -> None:
    navigation, modals, backend = started()
    await fed(navigation, modals, backend, CATALOG, view(OLD))
    navigation.place.offer()
    assert modals.offers
    await fed(navigation, modals, backend, opened(OLD))
    assert modals.offers == []
    navigation.place.offer()
    assert (modals.offers, changes(backend)) == ([], [OPENED])


async def test_the_viewing_mark_names_what_the_active_session_runs_until_it_is_idle() -> None:
    navigation, modals, backend = started()
    await fed(navigation, modals, backend, CATALOG, view(OLD), RUNNING_T3)
    assert navigation.place.mark == (
        "viewing auth rework; today is running a task (enter to switch when it's idle)"
    )
    await fed(navigation, modals, backend, IDLE)
    assert navigation.place.mark == VIEWING_OLD


async def test_the_viewing_mark_names_a_review() -> None:
    navigation, modals, backend = started()
    await fed(navigation, modals, backend, CATALOG, view(OLD), REVIEWING_T3)
    assert navigation.place.mark == (
        "viewing auth rework; today is running the review of a task"
        " (enter to switch when it's idle)"
    )


async def test_the_offer_names_a_task_the_active_session_runs() -> None:
    question = await offered_after(CATALOG, view(OLD), RUNNING_T3)
    assert question.message == "today is running a task; you can switch once it's idle."


async def test_a_pick_held_back_by_a_running_session_is_offered_again_once_it_is_idle() -> None:
    navigation, modals, backend = joined()
    await fed(navigation, modals, backend, CATALOG, RUNNING_T3)
    await picked_old(navigation, modals, backend)
    await fed(navigation, modals, backend, view(OLD))
    await modals.answer_offer(0)
    await fed(navigation, modals, backend, IDLE)
    question = offer(modals)
    assert question.message == "today is idle now."
    assert list(question.choices) == ACTIVATE
    await modals.answer_offer(0)
    assert changes(backend) == SWITCH_TO_OLD


async def test_the_offer_again_replaces_the_one_still_shown_and_comes_once() -> None:
    navigation, modals, backend = joined()
    await fed(navigation, modals, backend, CATALOG, RUNNING_T3)
    await picked_old(navigation, modals, backend)
    await fed(navigation, modals, backend, view(OLD), IDLE)
    assert offer(modals).message == "today is idle now."
    await modals.answer_offer(None)
    await fed(navigation, modals, backend, RUNNING_T3, IDLE)
    assert modals.offers == []
    assert changes(backend) == [Attach(workspace="w", session="old")]


async def test_a_view_not_picked_here_is_not_offered_again_when_the_active_one_is_idle() -> None:
    navigation, modals, backend = started()
    await fed(navigation, modals, backend, CATALOG, RUNNING_T3, view(OLD), IDLE)
    assert modals.offers == []
    assert navigation.place.mark == VIEWING_OLD


async def test_the_viewing_mark_says_the_active_session_waits_for_a_coder() -> None:
    waiting = (
        Activity(kind="task_run", task_id="t3"),
        Activity(kind="waiting_for_coder", position=2, busy=4),
    )
    navigation, modals, backend = started()
    state = RUNNING.model_copy(update={"activity": waiting})
    await fed(navigation, modals, backend, CATALOG, view(OLD), state)
    assert navigation.place.mark == (
        "viewing auth rework; today is waiting for a coder: 2nd in line"
        " (enter to switch when it's idle)"
    )
    assert navigation.place.coder_wait is None  # Auth rework, the session viewed, doesn't wait.
