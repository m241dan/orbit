"""The workspace picker's rows, labels and hints."""

from orbit.protocol import AddWorkspace, SessionStateChanged, SessionSummary, WorkspaceEntry
from orbit.tui.view.workspaces import (
    ADD_PATH,
    LIST_HINT,
    PATH_HINT,
    STARTUP_HINT,
    added_workspace,
    preselected,
    remove_message,
    row_key,
    row_label,
    with_states,
    workspace_label,
    workspace_rows,
    workspaces_hint,
)

TODAY = SessionSummary(id="s", name="today", active=True)
EARLIER = SessionSummary(id="e", name="earlier")
A = WorkspaceEntry(id="a", label="repoA", path="/src/repoA", sessions=(EARLIER, TODAY))
B = WorkspaceEntry(id="b", label="repoB", path="/src/repoB", sessions=())


def test_rows_list_the_served_workspaces_then_adding_a_path() -> None:
    assert workspace_rows((A, B), "/src/repoA") == (A, B, ADD_PATH)


def test_rows_offer_to_add_the_repository_first_while_it_is_not_served() -> None:
    assert workspace_rows((A, B), "/src/repoC") == (AddWorkspace(path="/src/repoC"), A, B, ADD_PATH)


def test_rows_without_a_repository_offer_none_to_add() -> None:
    assert workspace_rows((A,), None) == (A, ADD_PATH)


def test_the_workspace_at_the_repository_is_preselected() -> None:
    assert preselected((A, B, ADD_PATH), "/src/repoB") == 1


def test_without_one_at_the_repository_the_first_row_is_preselected() -> None:
    assert preselected((AddWorkspace(path="/src/repoC"), A, ADD_PATH), "/src/repoC") == 0
    assert preselected((A, B, ADD_PATH), None) == 0


def test_a_workspace_row_names_its_path_and_active_session_with_its_state() -> None:
    assert workspace_label(A) == "repoA · /src/repoA · today, idle"


def test_a_running_read_only_workspace_says_so() -> None:
    running = TODAY.model_copy(update={"state": "running"})
    entry = A.model_copy(update={"sessions": (running,), "read_only": True})
    assert workspace_label(entry) == "repoA · /src/repoA · today, running · read-only"


def test_a_workspace_without_a_path_or_active_session_shows_its_label() -> None:
    assert workspace_label(WorkspaceEntry(id="w", label="here", sessions=())) == "here"


def test_row_labels() -> None:
    assert row_label(AddWorkspace(path="/src/repoC")) == "Add /src/repoC"
    assert row_label(B) == "repoB · /src/repoB"
    assert row_label(ADD_PATH) == "Add a path…"


def test_removing_asks_about_the_workspace_by_its_label() -> None:
    assert remove_message(A) == (
        'Stop serving "repoA"? Its sessions are kept, for when it\'s added again.'
    )


def test_the_workspace_added_is_the_one_at_its_path() -> None:
    assert added_workspace((A, B), "/src/repoB", {"a", "b"}) == B


def test_the_workspace_added_is_else_the_new_one() -> None:
    assert added_workspace((A, B), "/src/repoB/sub", {"a"}) == B


def test_no_workspace_is_added_until_one_is_listed() -> None:
    assert added_workspace((A,), "/src/repoB/sub", {"a"}) is None


def test_session_states_change_in_every_workspace() -> None:
    changed = SessionStateChanged(session="s", active=True, state="running", viewers=1)
    [a, b] = with_states((A, B), changed)
    assert a.sessions[1].state == "running" and a.sessions[0] == EARLIER
    assert b == B


def test_hints() -> None:
    assert workspaces_hint(startup=False, adding=False) == LIST_HINT
    assert workspaces_hint(startup=True, adding=False) == STARTUP_HINT
    assert workspaces_hint(startup=True, adding=True) == PATH_HINT


def test_row_keys_tell_rows_apart_by_workspace_or_path() -> None:
    assert row_key(A) == row_key(A.model_copy(update={"sessions": ()})) == "a"
    assert row_key(AddWorkspace(path="/src/a")) == "add /src/a"
    assert row_key(ADD_PATH) == "add_path"
