"""The picker of the workspaces a control plane serves, without the screen showing it: its rows
and highlight, adding by path or URL, the clones under way, and removing workspaces."""

from collections.abc import Sequence

from orbit.protocol import (
    CONTROL_PLANE_STREAM,
    Activity,
    AddWorkspace,
    AgentRef,
    ListDirectory,
    RemoveWorkspace,
    SessionSummary,
    Watch,
    WorkspaceCloning,
    WorkspaceEntry,
)
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.typing_list import Label
from orbit.tui.view.workspace_picker import Answered, Explored, Question, WorkspacePicker
from tests.desk_fakes import Backend

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
PROJ_URL = "https://github.com/o/proj.git"
PROJ = WorkspaceEntry(id="P", label="proj", path="/srv/clones/proj", managed=True, sessions=())
LIST_HINT = "j/k move · enter open · x remove · esc close"
STARTUP_HINT = "j/k move · enter open · x remove · esc quit"


def cloned(phase: str, percent: int | None = None) -> WorkspaceCloning:
    """The news of the clone of ``PROJ_URL``: at ``phase``, ``percent`` through it."""
    return WorkspaceCloning(url=PROJ_URL, name="proj", phase=phase, percent=percent)


def agent(workspace: str, role: str, number: int) -> AgentRef:
    return AgentRef(
        workspace=workspace, session="s", agent_id=f"agent-{number}", role=role, task_id="t"
    )


Asked = tuple[Question, Answered]


class View:
    """What the workspace picker shows, as it shows it, and the screens it opens: the highlight
    is set as a test moves it."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend
        self.is_attached = True
        self.is_mounted = True
        self.is_active = True
        self.highlighted: int | None = None
        self.keys: list[str | None] = []
        self.rows: list[str] = []
        self.hint = ""
        self.note: str | None = None
        self.box: str | None = None  # The text box's text, while it's shown.
        self.box_note = ""
        self.dismissed: list[str | None] = []
        self.quit_count = 0
        self.explored: list[Explored] = []
        self.asked: list[Asked] = []

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

    def dismiss(self, result: str | None) -> None:
        self.dismissed.append(result)

    def quit(self) -> None:
        self.quit_count += 1

    def explore(self, explored: Explored) -> None:
        self.explored.append(explored)

    def ask(self, question: Question, answered: Answered) -> None:
        self.asked.append((question, answered))

    async def change(
        self, command: AddWorkspace | RemoveWorkspace | Watch | ListDirectory, awaited: Awaited
    ) -> object:
        return await self._backend.desk.send(command, awaited)


async def picker(
    workspaces: Sequence[WorkspaceEntry] = (B, A),
    repository: str | None = "/src/repoA",
    startup: bool = True,
) -> tuple[WorkspacePicker, View, Backend]:
    """A picker of ``workspaces`` for a frontend in ``repository``, opened as at ``startup``,
    started, its preselected row highlighted as the screen does."""
    backend = Backend()
    view = View(backend)
    picked = WorkspacePicker(view, workspaces, repository, startup)
    view.highlighted = picked.preselected
    await picked.start()
    return picked, view, backend


def labels(picked: WorkspacePicker) -> list[str]:
    return list(picked.labels)


def changes(backend: Backend) -> list[object]:
    """The commands sent, but for watching streams."""
    return [command for command in backend.commands if not isinstance(command, Watch)]


async def test_the_picker_preselects_the_workspace_at_the_repository_and_watches() -> None:
    picked, view, backend = await picker()
    assert picked.startup
    assert labels(picked) == [B_ROW, A_ROW, ADD_ROW]
    assert picked.keys == ("B", "A", None)
    assert view.highlighted == 1
    assert backend.commands == [Watch(streams=(CONTROL_PLANE_STREAM,))]


async def test_without_a_repository_served_the_first_workspace_is_preselected() -> None:
    picked, view, _ = await picker(repository=None)
    assert labels(picked) == [B_ROW, A_ROW, ADD_ROW]
    assert view.highlighted == 0


async def test_each_workspace_row_shows_its_running_agents_by_role() -> None:
    picked, view, _ = await picker()
    picked.show_agents((agent("A", "coder", 1), agent("A", "reviewer", 2), agent("A", "coder", 3)))
    assert view.rows == [B_ROW, f"{A_ROW} · 2 coders · 1 reviewer", ADD_ROW]
    assert view.highlighted == 1
    picked.show_agents(())
    assert view.rows == [B_ROW, A_ROW, ADD_ROW]


async def test_agents_shown_before_the_picker_is_mounted_show_once_it_is() -> None:
    picked, view, _ = await picker((A,), None)
    view.is_mounted = False
    running = (agent("A", "coder", 1),)
    picked.show_agents(running)
    assert picked.agents == running
    assert view.rows == []
    assert labels(picked)[0] == "repoA · /src/repoA · today, idle · 1 coder"


async def test_a_workspace_row_says_where_its_session_waits_for_a_coder() -> None:
    wait = Activity(kind="waiting_for_coder", position=2)
    running = TODAY_B.model_copy(update={"state": "running", "activity": (wait,)})
    picked, _, _ = await picker((B.model_copy(update={"sessions": (running,)}), A))
    assert labels(picked)[0] == "repoB · /src/repoB · flags, running · waiting: 2nd"


async def test_clones_show_after_the_workspaces_with_their_progress() -> None:
    picked, view, _ = await picker()
    picked.show((B, A), (cloned("Counting objects"),))
    assert view.rows[2] == "Cloning proj… Counting objects"
    picked.show((B, A), (cloned("Receiving objects", percent=42),))
    assert view.rows == [B_ROW, A_ROW, "Cloning proj… 42%", ADD_ROW]
    picked.show((B, A, PROJ), (cloned("Resolving"),))
    assert view.rows[2:] == ["proj · /srv/clones/proj", "Cloning proj… Resolving", ADD_ROW]


async def test_picking_or_removing_a_clones_row_does_nothing() -> None:
    picked, view, backend = await picker()
    picked.show((B, A), (cloned("Resolving"),))
    view.highlighted = 2
    await picked.choose(2)
    picked.remove()
    assert (view.dismissed, view.explored, view.asked, view.box) == ([], [], [], None)
    assert changes(backend) == []


async def test_picking_a_workspace_closes_with_it() -> None:
    picked, view, _ = await picker()
    await picked.choose(1)
    assert view.dismissed == ["A"]


async def test_picking_add_a_path_opens_the_explorer_whose_pick_is_taken() -> None:
    picked, view, backend = await picker()
    await picked.choose(2)
    [explored] = view.explored
    await explored(None)
    assert view.dismissed == []
    await explored("B")
    assert view.dismissed == ["B"]
    await explored(AddWorkspace(path="/src/repoC"))
    picked.show((B, A, C))
    assert view.dismissed == ["B", "C"]
    assert changes(backend) == []


async def test_an_explorer_add_of_a_url_awaits_its_clone() -> None:
    picked, view, _ = await picker()
    await picked.choose(2)
    await view.explored[0](AddWorkspace(url=PROJ_URL))
    picked.clone_failed(PROJ_URL, "Couldn't clone it.")
    assert (view.box, view.box_note) == (PROJ_URL, "Couldn't clone it.")


async def test_a_url_typed_is_sent_as_one_closing_the_box_once_accepted() -> None:
    for url in (PROJ_URL, "git@github.com:o/r.git", "file:///x/r.git"):
        picked, view, backend = await picker()
        picked.type_path()
        assert (view.box, view.hint) == ("", "enter add · esc back")
        await picked.typed(url)
        assert changes(backend) == [AddWorkspace(url=url)]
        backend.accept()
        assert view.box is None
        assert view.hint == STARTUP_HINT


async def test_a_clone_failing_shows_its_message_in_the_box_with_the_url_back() -> None:
    picked, view, backend = await picker()
    picked.type_path()
    await picked.typed(PROJ_URL)
    backend.accept()
    picked.clone_failed(PROJ_URL, f"Couldn't clone {PROJ_URL}: Repository not found.")
    assert view.box == PROJ_URL
    assert view.box_note == f"Couldn't clone {PROJ_URL}: Repository not found."
    assert view.hint == "enter add · esc back"


async def test_another_clone_failing_leaves_the_box_alone() -> None:
    picked, view, backend = await picker()
    picked.type_path()
    await picked.typed(PROJ_URL)
    backend.accept()
    picked.clone_failed("git@host:o/other.git", "Couldn't clone git@host:o/other.git: denied")
    assert (view.box, view.box_note) == (None, "")


async def test_a_clone_fails_only_once() -> None:
    picked, view, backend = await picker()
    picked.type_path()
    await picked.typed(PROJ_URL)
    backend.accept()
    picked.clone_failed(PROJ_URL, "first")
    picked.back()
    picked.clone_failed(PROJ_URL, "again")
    assert view.box is None


async def test_a_repository_not_served_is_offered_first_and_picked_once_added() -> None:
    picked, view, backend = await picker(repository="/src/repoC")
    assert labels(picked) == ["Add /src/repoC", B_ROW, A_ROW, ADD_ROW]
    assert view.highlighted == 0
    await picked.choose(0)
    assert changes(backend) == [AddWorkspace(path="/src/repoC")]
    backend.accept()
    assert view.dismissed == []
    picked.show((B, A, C))
    assert view.dismissed == ["C"]


async def test_a_workspace_added_and_listed_before_its_acceptance_is_picked_on_it() -> None:
    picked, view, backend = await picker(repository="/src/repoC")
    await picked.choose(0)
    picked.show((B, A, C))
    assert view.dismissed == []
    assert view.rows[-2] == "repoC · /src/repoC · new, idle"
    backend.accept()
    assert view.dismissed == ["C"]


async def test_adding_a_path_sends_it_and_a_refusal_shows_in_the_boxs_hint() -> None:
    picked, view, backend = await picker()
    picked.type_path()
    await picked.typed("/nowhere")
    assert changes(backend) == [AddWorkspace(path="/nowhere")]
    backend.refuse("There's no /nowhere.")
    assert (view.box_note, view.note, backend.shown) == ("There's no /nowhere.", None, [])


async def test_a_path_added_picks_the_workspace_it_adds_once_listed() -> None:
    cases: list[tuple[Sequence[WorkspaceEntry], str, Sequence[WorkspaceEntry], str]] = [
        ((B, A), "/src/repoC/sub", (B, A, C), "C"),
        ((B, A), "/src/repoA/sub", (B, A), "A"),
        ((B, A), "/src/repoA/", (B, A), "A"),
        ((A,), "/src/repoC/sub", (B, A, C), "C"),
    ]
    for before, path, after, chosen in cases:
        picked, view, backend = await picker(before)
        picked.type_path()
        await picked.typed(path)
        assert changes(backend) == [AddWorkspace(path=path)]
        backend.accept()
        picked.show(after)
        assert view.dismissed == [chosen]


async def test_a_path_leaving_a_workspace_by_dot_dot_picks_the_one_it_names() -> None:
    picked, view, backend = await picker((A,))
    picked.type_path()
    await picked.typed("/src/repoA/../repoB")
    backend.accept()
    assert view.dismissed == []
    picked.show((A, B))
    assert view.dismissed == ["B"]


async def test_another_workspace_listed_after_adding_one_served_is_not_picked() -> None:
    picked, view, backend = await picker()
    picked.type_path()
    await picked.typed("/home/me/repoA")
    backend.accept()
    picked.show((B, A, C))
    assert view.dismissed == []
    assert view.rows[-2] == "repoC · /src/repoC · new, idle"


async def test_an_empty_path_sends_nothing_and_escape_goes_back_to_the_list() -> None:
    picked, view, backend = await picker()
    picked.type_path()
    await picked.typed(" ")
    assert changes(backend) == []
    picked.back()
    assert view.box is None
    assert (view.quit_count, view.dismissed) == (0, [])


async def test_escape_on_the_list_quits_at_startup_and_closes_otherwise() -> None:
    picked, view, _ = await picker()
    picked.back()
    assert (view.quit_count, view.dismissed) == (1, [])
    picked, view, _ = await picker(startup=False)
    picked.back()
    assert (view.quit_count, view.dismissed) == (0, [None])


async def test_x_keeps_a_workspaces_sessions_by_default_and_a_busy_refusal_shows() -> None:
    picked, view, backend = await picker()
    view.highlighted = 0
    picked.remove()
    [(question, answered)] = view.asked
    assert question == Question(
        "Stop serving repoB",
        "Its sessions can be kept, for when it's added again, or deleted with it.",
        ("Keep its sessions", "Delete its sessions too", "Cancel"),
        2,
    )
    await answered(0)
    assert changes(backend) == [RemoveWorkspace(workspace="B", purge=False)]
    backend.refuse("Can't remove /src/repoB: it's running task 3.")
    assert view.note == "Can't remove /src/repoB: it's running task 3."


async def test_x_then_deleting_its_sessions_purges_and_a_refusal_lists_what_is_lost() -> None:
    picked, view, backend = await picker((A, PROJ))
    view.highlighted = 1
    picked.remove()
    question, answered = view.asked[0]
    assert question.choices[1] == (
        "Delete its sessions too, and its clone directory /srv/clones/proj"
    )
    await answered(1)
    assert changes(backend) == [RemoveWorkspace(workspace="P", purge=True)]
    backend.refuse("Can't purge proj: its clone has 2 commits its remote doesn't.")
    assert view.note == "Can't purge proj: its clone has 2 commits its remote doesn't."


async def test_x_cancelled_removes_nothing() -> None:
    for answer in (2, None):
        picked, view, backend = await picker()
        picked.remove()
        await view.asked[0][1](answer)
        assert changes(backend) == []
        assert view.dismissed == []


async def test_x_on_a_row_adding_a_workspace_does_nothing() -> None:
    picked, view, _ = await picker(repository="/src/repoC")
    picked.remove()
    assert view.asked == []


async def test_a_refused_add_of_the_repository_shows_under_the_list() -> None:
    picked, view, backend = await picker(repository="/src/repoC")
    await picked.choose(0)
    backend.refuse("gone")
    assert view.note == "gone"


async def test_a_removal_listed_keeps_the_highlight_on_its_row() -> None:
    picked, view, _ = await picker()
    picked.show((A,))
    assert view.rows == [A_ROW, ADD_ROW]
    assert view.highlighted == 0
    picked.show((C, A))
    assert view.highlighted == 1


async def test_a_gone_row_highlighted_moves_to_where_it_was() -> None:
    picked, view, _ = await picker((B, A, C), None)
    view.highlighted = 1
    picked.show((B, C))
    assert view.highlighted == 1


async def test_removing_the_last_workspace_highlighted_moves_to_the_one_above() -> None:
    picked, view, _ = await picker(repository=None)
    view.highlighted = 1
    picked.show((B,))
    assert view.rows == [B_ROW, ADD_ROW]
    assert view.highlighted == 0


async def test_listing_before_the_picker_is_mounted_only_keeps_the_workspaces() -> None:
    picked, view, _ = await picker()
    view.is_mounted = False
    picked.show((A,))
    assert picked.workspaces == (A,)
    assert view.rows == []


async def test_an_add_answered_once_the_picker_closed_changes_nothing() -> None:
    for refused in (None, "gone"):
        picked, view, backend = await picker(repository="/src/repoC")
        await picked.choose(0)
        view.is_attached = False
        if refused is None:
            backend.accept()
        else:
            backend.refuse(refused)
        picked.show((B, A, C))
        assert (view.dismissed, view.note, backend.shown) == ([], None, [])


async def test_an_add_listed_while_another_screen_is_over_the_picker_is_picked_after() -> None:
    picked, view, backend = await picker(repository="/src/repoC")
    await picked.choose(0)
    backend.accept()
    view.is_active = False
    picked.show((B, A, C))
    assert view.dismissed == []
    view.is_active = True
    picked.show((B, A, C))
    assert view.dismissed == ["C"]


async def test_must_pick_has_escape_quit() -> None:
    picked, view, _ = await picker(startup=False)
    picked.must_pick()
    assert picked.startup
    assert view.hint == STARTUP_HINT
    picked.back()
    assert view.quit_count == 1


async def test_the_list_hint_names_escape_as_closing_unless_at_startup() -> None:
    picked, _, _ = await picker(startup=False)
    assert picked.hint == LIST_HINT
