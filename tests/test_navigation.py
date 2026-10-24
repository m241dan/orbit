"""Where a frontend is and how it moves, without Textual: the workspace picker at startup over a
link and when asked, adding and removing workspaces, clones, and moving between workspaces and
their sessions."""

from orbit.protocol import (
    BUSY,
    CONTROL_PLANE_STREAM,
    GIT_FAILED,
    AddWorkspace,
    AgentRef,
    AgentsChanged,
    Attach,
    Catalog,
    DirectoryListing,
    ErrorOccurred,
    NewSession,
    RemoveWorkspace,
    SessionRef,
    SessionStateChanged,
    SessionSummary,
    Watch,
    WorkspaceCloning,
    WorkspaceEntry,
)
from orbit.tui.view.navigation import Navigation
from orbit.tui.view.opening import Opening
from orbit.tui.view.session_picker import SessionPicker
from orbit.tui.view.workspace_picker import WorkspacePicker
from tests.desk_fakes import Backend
from tests.navigation_fakes import Modals, accept_change, changes, fed, navigating
from tests.tui_fakes import opened

TODAY_A = SessionSummary(id="a1", name="today", active=True)
EARLIER_A = SessionSummary(id="a0", name="earlier")
TODAY_B = SessionSummary(id="b1", name="flags", active=True)
TODAY_C = SessionSummary(id="c1", name="new", active=True)
A = WorkspaceEntry(id="A", label="repoA", path="/src/repoA", sessions=(TODAY_A, EARLIER_A))
B = WorkspaceEntry(id="B", label="repoB", path="/src/repoB", sessions=(TODAY_B,))
C = WorkspaceEntry(id="C", label="repoC", path="/src/repoC", sessions=(TODAY_C,))
A_ROW = "repoA · /src/repoA · today, idle"
B_ROW = "repoB · /src/repoB · flags, idle"
ADD_ROW = "Add a path or URL…"
STARTUP_HINT = "j/k move · enter open · x remove · esc quit"
WATCHING = Watch(streams=(CONTROL_PLANE_STREAM,))
PROJ_URL = "https://github.com/o/proj.git"
PROJ = WorkspaceEntry(id="P", label="proj", path="/srv/clones/proj", managed=True, sessions=())
AUTH = Opening(NewSession(name="auth"), repository="/src/repoA")


def catalog(*workspaces: WorkspaceEntry) -> Catalog:
    return Catalog(workspaces=workspaces, agents=(), triggers=(), lanes=())


SERVED = catalog(B, A)


def cloned(
    phase: str, percent: int | None = None, workspace: str | None = None
) -> WorkspaceCloning:
    """The news of the clone of ``PROJ_URL``: at ``phase``, ``percent`` through it, or served as
    ``workspace``."""
    return WorkspaceCloning(
        url=PROJ_URL, name="proj", phase=phase, percent=percent, workspace=workspace
    )


def agent(workspace: str, role: str, number: int) -> AgentRef:
    return AgentRef(
        workspace=workspace, session="s", agent_id=f"agent-{number}", role=role, task_id="t"
    )


def linked(
    repository: str | None = "/src/repoA", opening: Opening | None = None
) -> tuple[Navigation, Modals, Backend]:
    """A frontend linked to a control plane, run in ``repository``, picking a session at startup
    unless ``opening`` says otherwise."""
    return navigating(opening or Opening(pick=True, repository=repository), linked=True)


def workspaces(modals: Modals) -> WorkspacePicker:
    picker = modals.workspaces
    assert picker is not None
    return picker


def sessions(modals: Modals) -> SessionPicker:
    picker = modals.sessions
    assert picker is not None
    return picker


def workspace_rows(modals: Modals) -> list[str]:
    assert modals.workspaces_view is not None
    return modals.workspaces_view.rows


def session_rows(modals: Modals) -> list[str]:
    assert modals.sessions_view is not None
    return modals.sessions_view.rows


async def pick_workspace(modals: Modals, index: int | None = None) -> None:
    """Pick the workspace row at ``index``, the one highlighted by default, as enter does."""
    view = modals.workspaces_view
    assert view is not None
    chosen = view.highlighted if index is None else index
    assert chosen is not None
    await workspaces(modals).choose(chosen)
    await modals.settle()


async def pick_session(modals: Modals, index: int | None = None) -> None:
    """Pick the session row at ``index``, the one highlighted by default, as enter does."""
    view = modals.sessions_view
    assert view is not None
    chosen = view.highlighted if index is None else index
    assert chosen is not None
    sessions(modals).choose(chosen)
    await modals.settle()


async def back(picker: WorkspacePicker | SessionPicker, modals: Modals) -> None:
    """Escape from ``picker``."""
    picker.back()
    await modals.settle()


async def joined_a(navigation: Navigation, modals: Modals, backend: Backend) -> None:
    """Join repoA's active session from the startup pickers."""
    await fed(navigation, modals, backend, SERVED)
    await modals.settle()
    await pick_workspace(modals)
    await pick_session(modals)


async def opened_workspaces(navigation: Navigation, modals: Modals) -> None:
    """Open the workspace picker, as ctrl+w does."""
    navigation.workspaces.open()
    await modals.settle()


def watches(backend: Backend) -> list[Watch]:
    """The streams the workspace picker was told to watch, in turn."""
    return [command for command in backend.commands if isinstance(command, Watch)]


async def test_the_picker_watches_the_control_plane_while_it_is_open() -> None:
    navigation, modals, backend = linked()
    await fed(navigation, modals, backend, SERVED)
    await modals.settle()
    assert watches(backend) == [WATCHING]
    await pick_workspace(modals)
    await pick_session(modals)
    assert watches(backend) == [WATCHING, Watch(streams=())]
    await opened_workspaces(navigation, modals)
    assert watches(backend) == [WATCHING, Watch(streams=()), WATCHING]
    await back(workspaces(modals), modals)
    assert watches(backend) == [WATCHING, Watch(streams=()), WATCHING, Watch(streams=())]


async def test_the_startup_picker_lists_the_workspaces_with_the_repositorys_highlighted() -> None:
    navigation, modals, backend = linked()
    await fed(navigation, modals, backend, SERVED)
    picker = workspaces(modals)
    assert picker.startup
    assert workspace_rows(modals) == [B_ROW, A_ROW, ADD_ROW]
    assert modals.workspaces_view is not None and modals.workspaces_view.highlighted == 1
    assert changes(backend) == []


async def test_each_workspace_row_shows_its_running_agents_by_role() -> None:
    navigation, modals, backend = linked()
    await fed(navigation, modals, backend, SERVED)
    running = (agent("A", "coder", 1), agent("A", "reviewer", 2), agent("A", "coder", 3))
    await fed(navigation, modals, backend, AgentsChanged(agents=running))
    assert workspace_rows(modals) == [B_ROW, f"{A_ROW} · 2 coders · 1 reviewer", ADD_ROW]
    await fed(navigation, modals, backend, AgentsChanged(agents=()))
    assert workspace_rows(modals) == [B_ROW, A_ROW, ADD_ROW]


async def test_a_picker_opened_again_shows_no_agents_until_they_are_sent_again() -> None:
    navigation, modals, backend = linked()
    await fed(navigation, modals, backend, SERVED)
    await fed(navigation, modals, backend, AgentsChanged(agents=(agent("B", "planner", 1),)))
    await pick_workspace(modals)
    await pick_session(modals)
    await opened_workspaces(navigation, modals)
    assert workspace_rows(modals) == [B_ROW, A_ROW, ADD_ROW]


async def test_agents_changing_while_the_picker_is_closed_show_nothing() -> None:
    navigation, modals, backend = linked()
    await joined_a(navigation, modals, backend)
    await fed(navigation, modals, backend, AgentsChanged(agents=(agent("B", "coder", 1),)))
    await opened_workspaces(navigation, modals)
    assert workspace_rows(modals) == [B_ROW, A_ROW, ADD_ROW]


async def test_a_directory_listing_with_no_explorer_open_changes_nothing_shown() -> None:
    navigation, modals, backend = linked()
    await fed(navigation, modals, backend, SERVED)
    await fed(navigation, modals, backend, DirectoryListing(path="/srv", entries=(), total=0))
    assert workspace_rows(modals) == [B_ROW, A_ROW, ADD_ROW]


async def test_a_directory_listing_reaches_the_explorer_open() -> None:
    navigation, modals, backend = linked()
    await fed(navigation, modals, backend, SERVED)
    await pick_workspace(modals, 2)
    await fed(navigation, modals, backend, DirectoryListing(path="/srv", entries=(), total=0))
    assert modals.explorer_view is not None and modals.explorer_view.title == "/srv"


async def test_a_clones_progress_shows_in_its_row_until_it_is_served() -> None:
    navigation, modals, backend = linked()
    await fed(navigation, modals, backend, SERVED)
    await fed(navigation, modals, backend, cloned("Counting objects"))
    assert workspace_rows(modals)[2] == "Cloning proj… Counting objects"
    await fed(navigation, modals, backend, cloned("Receiving objects", percent=42))
    assert workspace_rows(modals) == [B_ROW, A_ROW, "Cloning proj… 42%", ADD_ROW]
    await fed(navigation, modals, backend, catalog(B, A, PROJ))
    await fed(navigation, modals, backend, cloned("Done", percent=100, workspace="P"))
    assert workspace_rows(modals)[2:] == ["proj · /srv/clones/proj", ADD_ROW]


async def test_a_clone_under_way_shows_in_a_picker_opened_later() -> None:
    navigation, modals, backend = linked()
    await joined_a(navigation, modals, backend)
    await fed(navigation, modals, backend, cloned("Resolving"))
    await opened_workspaces(navigation, modals)
    assert workspace_rows(modals)[2] == "Cloning proj… Resolving"


async def test_a_url_typed_is_sent_as_one_closing_the_box_once_accepted() -> None:
    navigation, modals, backend = linked()
    await fed(navigation, modals, backend, SERVED)
    picker = workspaces(modals)
    picker.type_path()
    await picker.typed(PROJ_URL)
    assert changes(backend) == [AddWorkspace(url=PROJ_URL)]
    accept_change(backend)
    assert modals.workspaces_view is not None and modals.workspaces_view.box is None


async def test_a_clone_failing_drops_its_row_and_shows_gits_message_in_the_boxs_hint() -> None:
    for code in (GIT_FAILED, "invalid", "internal"):
        navigation, modals, backend = linked()
        await fed(navigation, modals, backend, SERVED)
        picker = workspaces(modals)
        picker.type_path()
        await picker.typed(PROJ_URL)
        accept_change(backend)
        await fed(navigation, modals, backend, cloned("Resolving"))
        failed = f"Couldn't clone {PROJ_URL}: Repository not found."
        await fed(
            navigation, modals, backend, ErrorOccurred(code=code, message=failed, url=PROJ_URL)
        )
        view = modals.workspaces_view
        assert view is not None
        assert view.rows == [B_ROW, A_ROW, ADD_ROW]
        assert (view.box, view.box_note) == (PROJ_URL, failed)


async def test_another_clone_failing_or_another_error_leaves_the_box_alone() -> None:
    navigation, modals, backend = linked()
    await fed(navigation, modals, backend, SERVED)
    picker = workspaces(modals)
    picker.type_path()
    await picker.typed(PROJ_URL)
    accept_change(backend)
    await fed(navigation, modals, backend, cloned("Resolving"))
    other = "Couldn't clone git@host:o/other.git: denied"
    failed = ErrorOccurred(code=GIT_FAILED, message=other, url="git@host:o/other.git")
    await fed(navigation, modals, backend, failed)
    await fed(navigation, modals, backend, ErrorOccurred(code="unknown", message=f"{PROJ_URL}: "))
    view = modals.workspaces_view
    assert view is not None
    assert view.rows[2] == "Cloning proj… Resolving"
    assert view.box is None


async def test_a_clone_failing_with_the_picker_closed_drops_its_row() -> None:
    navigation, modals, backend = linked()
    await joined_a(navigation, modals, backend)
    await fed(navigation, modals, backend, cloned("Resolving"))
    failed = f"Couldn't clone {PROJ_URL}: Repository not found."
    await fed(
        navigation, modals, backend, ErrorOccurred(code=GIT_FAILED, message=failed, url=PROJ_URL)
    )
    await opened_workspaces(navigation, modals)
    assert workspace_rows(modals) == [B_ROW, A_ROW, ADD_ROW]


async def test_reconnecting_drops_the_clones_from_before() -> None:
    navigation, modals, backend = linked()
    await fed(navigation, modals, backend, SERVED)
    await fed(navigation, modals, backend, cloned("Resolving"))
    assert workspace_rows(modals)[2] == "Cloning proj… Resolving"
    navigation.reconnected()
    assert workspace_rows(modals) == [B_ROW, A_ROW, ADD_ROW]


async def test_picking_a_workspace_opens_its_session_picker_whose_pick_attaches() -> None:
    navigation, modals, backend = linked()
    await fed(navigation, modals, backend, SERVED)
    await pick_workspace(modals)
    picker = sessions(modals)
    assert picker.startup and picker.workspace == "A"
    assert session_rows(modals)[:2] == ["  Resume today · active", "  earlier"]
    await pick_session(modals, 1)
    assert changes(backend)[0] == Attach(workspace="A", session="a0")


async def test_resuming_attaches_to_the_active_session() -> None:
    navigation, modals, backend = linked()
    await joined_a(navigation, modals, backend)
    assert changes(backend) == [Attach(workspace="A", session="a1")]


async def test_a_repository_not_served_is_offered_first_and_added_then_its_sessions_shown() -> None:
    navigation, modals, backend = linked("/src/repoC")
    await fed(navigation, modals, backend, SERVED)
    assert workspace_rows(modals) == ["Add /src/repoC", B_ROW, A_ROW, ADD_ROW]
    await pick_workspace(modals)
    assert changes(backend) == [AddWorkspace(path="/src/repoC")]
    accept_change(backend)
    await fed(navigation, modals, backend, catalog(B, A, C))
    await modals.settle()
    assert sessions(modals).workspace == "C"
    await pick_session(modals)
    assert changes(backend)[-1] == Attach(workspace="C", session="c1")


async def test_a_busy_refusal_of_a_removal_shows_under_the_list() -> None:
    navigation, modals, backend = linked()
    await fed(navigation, modals, backend, SERVED)
    view = modals.workspaces_view
    assert view is not None
    view.highlighted = 0
    workspaces(modals).remove()
    [(question, answered)] = modals.questions
    assert question.title == "Stop serving repoB"
    await answered(0)
    assert changes(backend) == [RemoveWorkspace(workspace="B", purge=False)]
    busy = "Can't remove /src/repoB: it's running task 3."
    backend.refuse(busy, code=BUSY)
    assert view.note == busy


async def test_a_state_change_shows_in_the_workspaces_row() -> None:
    navigation, modals, backend = linked()
    running = SessionStateChanged(session="b1", active=True, state="running", viewers=1)
    await fed(navigation, modals, backend, SERVED, running)
    assert workspace_rows(modals)[0] == "repoB · /src/repoB · flags, running"


async def test_the_picker_opened_again_closes_on_escape() -> None:
    navigation, modals, backend = linked()
    await joined_a(navigation, modals, backend)
    await opened_workspaces(navigation, modals)
    picker = workspaces(modals)
    assert not picker.startup
    await back(picker, modals)
    assert modals.workspaces is None
    assert modals.workspaces_view is None


async def test_picking_the_workspace_viewed_opens_its_usual_session_picker() -> None:
    navigation, modals, backend = linked()
    await joined_a(navigation, modals, backend)
    await opened_workspaces(navigation, modals)
    await pick_workspace(modals)
    picker = sessions(modals)
    assert not picker.startup and picker.joined
    assert session_rows(modals)[0] == "● today · active"


async def moved_to_b(navigation: Navigation, modals: Modals, backend: Backend) -> None:
    """Join repoA, then open repoB's sessions from the workspace picker."""
    await joined_a(navigation, modals, backend)
    await opened_workspaces(navigation, modals)
    await pick_workspace(modals, 0)


async def test_moving_to_another_workspace_resumes_its_active_session_first() -> None:
    navigation, modals, backend = linked()
    await moved_to_b(navigation, modals, backend)
    picker = sessions(modals)
    assert picker.workspace == "B" and not picker.joined and not picker.startup
    assert session_rows(modals) == ["  Resume flags · active", "New session"]
    await pick_session(modals)
    assert changes(backend)[-1] == Attach(workspace="B", session="b1")
    navigation.place.open()
    assert sessions(modals).workspace == "B"


async def viewing_earlier_a(navigation: Navigation, modals: Modals, backend: Backend) -> None:
    """Join repoA at startup and view its earlier session read-only, then open repoB's
    sessions."""
    await joined_a(navigation, modals, backend)
    read_only = {"active": False, "open_session": SessionRef(id="a1", name="today")}
    await fed(navigation, modals, backend, opened(EARLIER_A).model_copy(update=read_only))
    assert navigation.place.mark is not None
    await opened_workspaces(navigation, modals)
    await pick_workspace(modals, 0)


async def test_moving_to_another_workspace_clears_the_viewing_mark_right_away() -> None:
    navigation, modals, backend = linked()
    await viewing_earlier_a(navigation, modals, backend)
    moves = modals.moves
    await pick_session(modals)
    assert navigation.place.mark is None
    assert modals.moves > moves


async def test_moving_to_another_workspace_failing_to_attach_stays_viewing_where_it_was() -> None:
    navigation, modals, backend = linked()
    await viewing_earlier_a(navigation, modals, backend)
    shown = navigation.place.mark
    backend.send_error = RuntimeError("the backend is gone")
    await pick_session(modals)
    assert navigation.place.mark == shown
    picker = sessions(modals)  # Shown again, saying why.
    assert modals.sessions_view is not None
    assert modals.sessions_view.note == "Couldn't send the command: the backend is gone"
    await back(picker, modals)
    navigation.place.open()
    assert sessions(modals).workspace == "A"


async def test_a_join_at_startup_failing_to_attach_joins_nothing_and_sends_no_opening() -> None:
    navigation, modals, backend = linked(opening=AUTH)
    await fed(navigation, modals, backend, SERVED)
    backend.send_error = RuntimeError("the backend is gone")
    await pick_workspace(modals)
    assert changes(backend) == [Attach(workspace="A", session="a1")]
    assert (navigation.place.workspace, navigation.place.attached) == (None, None)


async def test_a_new_session_in_another_workspace_attaches_there_first() -> None:
    navigation, modals, backend = linked()
    await moved_to_b(navigation, modals, backend)
    await pick_session(modals, 1)
    await sessions(modals).named("")
    await modals.settle()
    assert changes(backend)[-2:] == [Attach(workspace="B", session="b1"), NewSession()]


async def test_escape_from_another_workspaces_sessions_stays_on_the_workspace_viewed() -> None:
    navigation, modals, backend = linked()
    await moved_to_b(navigation, modals, backend)
    await back(sessions(modals), modals)
    navigation.place.open()
    assert sessions(modals).workspace == "A"
    assert changes(backend) == [Attach(workspace="A", session="a1")]


async def test_another_workspaces_sessions_follow_the_catalog_while_shown() -> None:
    navigation, modals, backend = linked()
    await moved_to_b(navigation, modals, backend)
    made = SessionSummary(id="b2", name="made")
    await fed(
        navigation, modals, backend, catalog(B.model_copy(update={"sessions": (TODAY_B, made)}), A)
    )
    assert session_rows(modals) == ["  Resume flags · active", "  made", "New session"]


async def test_the_workspace_viewed_removed_opens_a_picker_whose_escape_quits() -> None:
    navigation, modals, backend = linked()
    await joined_a(navigation, modals, backend)
    await fed(navigation, modals, backend, catalog(B))
    picker = workspaces(modals)
    assert workspace_rows(modals) == ["Add /src/repoA", B_ROW, ADD_ROW]
    assert picker.startup and picker.hint == STARTUP_HINT
    picker.back()
    assert modals.workspaces_view is not None and modals.workspaces_view.quit_count == 1


async def test_opening_with_a_command_attaches_to_the_workspace_picked_then_sends_it() -> None:
    navigation, modals, backend = linked(opening=AUTH)
    await fed(navigation, modals, backend, SERVED)
    await pick_workspace(modals)
    assert changes(backend) == [Attach(workspace="A", session="a1"), NewSession(name="auth")]
    assert modals.sessions is None


async def test_opening_with_a_command_on_a_workspace_without_an_active_session_sends_none() -> None:
    navigation, modals, backend = linked(opening=AUTH)
    await fed(navigation, modals, backend, catalog(A.model_copy(update={"sessions": (EARLIER_A,)})))
    await pick_workspace(modals)
    assert changes(backend) == []


async def test_an_add_accepted_once_the_picker_closed_opens_nothing() -> None:
    navigation, modals, backend = linked("/src/repoC")
    await fed(navigation, modals, backend, SERVED)
    await pick_workspace(modals, 1)
    await pick_session(modals)
    await opened_workspaces(navigation, modals)
    await pick_workspace(modals)
    await back(workspaces(modals), modals)
    accept_change(backend)
    await fed(navigation, modals, backend, catalog(B, A, C))
    await modals.settle()
    assert (modals.workspaces, modals.sessions) == (None, None)


async def removed_a_viewed(navigation: Navigation, modals: Modals, backend: Backend) -> None:
    """Join repoA, then remove it from the workspace picker, keeping its sessions, and have the
    catalog list only repoB."""
    await joined_a(navigation, modals, backend)
    await opened_workspaces(navigation, modals)
    workspaces(modals).remove()
    await modals.questions[-1][1](0)
    await fed(navigation, modals, backend, catalog(B))


async def test_removing_the_workspace_viewed_from_the_picker_keeps_one_picker_escape_quits() -> (
    None
):
    navigation, modals, backend = linked()
    await joined_a(navigation, modals, backend)
    await opened_workspaces(navigation, modals)
    picker = workspaces(modals)
    picker.remove()
    await modals.questions[-1][1](0)
    await fed(navigation, modals, backend, catalog(B))
    assert workspaces(modals) is picker
    assert changes(backend)[-1] == RemoveWorkspace(workspace="A")
    assert picker.startup and picker.hint == STARTUP_HINT
    picker.back()
    assert modals.workspaces_view is not None and modals.workspaces_view.quit_count == 1


async def test_picking_after_removing_the_workspace_viewed_opens_sessions_escape_quits() -> None:
    navigation, modals, backend = linked()
    await removed_a_viewed(navigation, modals, backend)
    await pick_workspace(modals, 1)
    picker = sessions(modals)
    assert picker.startup and picker.workspace == "B"
    picker.back()
    assert modals.sessions_view is not None and modals.sessions_view.quit_count == 1


async def picked_b_after_the_loss(navigation: Navigation, modals: Modals, backend: Backend) -> None:
    """Pick repoB in the picker the loss of repoA leaves shown."""
    await fed(navigation, modals, backend, catalog(B))
    await modals.settle()
    await pick_workspace(modals, 1)


def joined_b_once(modals: Modals, backend: Backend) -> None:
    """Check the frontend attached to repoB's active session, having sent auth only once."""
    sent = changes(backend)
    assert sent[-1] == Attach(workspace="B", session="b1")
    assert sent.count(NewSession(name="auth")) == 1
    assert modals.sessions is None


async def test_picking_after_removing_the_workspace_viewed_attaches_its_active_session() -> None:
    navigation, modals, backend = linked(opening=AUTH)
    await fed(navigation, modals, backend, SERVED)
    await pick_workspace(modals)
    await opened_workspaces(navigation, modals)
    workspaces(modals).remove()
    await modals.questions[-1][1](0)
    await picked_b_after_the_loss(navigation, modals, backend)
    joined_b_once(modals, backend)


async def test_picking_in_the_picker_a_loss_opens_attaches_its_active_session() -> None:
    navigation, modals, backend = linked(opening=AUTH)
    await fed(navigation, modals, backend, SERVED)
    await pick_workspace(modals)
    await opened_workspaces(navigation, modals)
    workspaces(modals).remove()
    await modals.questions[-1][1](0)
    await back(workspaces(modals), modals)
    assert modals.workspaces is None
    await picked_b_after_the_loss(navigation, modals, backend)
    joined_b_once(modals, backend)


async def test_picking_from_the_picker_without_a_loss_opens_sessions_escape_goes_back() -> None:
    navigation, modals, backend = linked()
    await moved_to_b(navigation, modals, backend)
    picker = sessions(modals)
    assert not picker.startup
    await back(picker, modals)
    assert (modals.sessions, modals.workspaces) == (None, None)
    assert changes(backend) == [Attach(workspace="A", session="a1")]
