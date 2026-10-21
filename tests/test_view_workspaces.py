"""The workspace picker's rows, labels and hints."""

from orbit.protocol import AddWorkspace, SessionStateChanged, SessionSummary, WorkspaceEntry
from orbit.tui.view.common import picker_hint
from orbit.tui.view.workspaces import (
    ADD_PATH,
    LIST_HINT,
    PATH_HINT,
    STARTUP_HINT,
    WORKSPACES_HINTS,
    added_workspace,
    preselected,
    remove_message,
    row_key,
    row_label,
    with_states,
    workspace_at,
    workspace_label,
    workspace_rows,
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


def test_without_a_repository_a_workspace_without_a_path_is_not_preselected() -> None:
    pathless = WorkspaceEntry(id="p", label="local", path=None, sessions=())
    assert preselected((A, pathless, ADD_PATH), None) == 0


def test_the_workspace_at_a_repository_is_the_one_at_its_path() -> None:
    assert workspace_at((A, B), "/src/repoB") == B
    assert workspace_at((A, B), "/src/repoC") is None


def test_no_workspace_is_at_no_repository_even_one_without_a_path() -> None:
    pathless = WorkspaceEntry(id="p", label="local", path=None, sessions=())
    assert workspace_at((pathless, A), None) is None


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
    assert added_workspace((A, B), "/src/repoB") == B


def test_a_new_workspace_elsewhere_is_not_the_one_added() -> None:
    assert added_workspace((A, B), "/src/repoC/sub") is None


def test_no_workspace_is_added_until_one_is_listed() -> None:
    assert added_workspace((A,), "/src/repoB/sub") is None


def test_the_workspace_added_is_the_one_containing_the_path() -> None:
    assert added_workspace((B, A), "/src/repoA/sub") == A
    assert added_workspace((B, A), "/src/repoA/") == A


def test_the_deepest_workspace_containing_the_path_is_added() -> None:
    inner = WorkspaceEntry(id="i", label="inner", path="/src/repoA/inner", sessions=())
    assert added_workspace((A, inner), "/src/repoA/inner/sub") == inner


def test_a_sibling_with_a_longer_name_does_not_contain_the_path() -> None:
    sibling = WorkspaceEntry(id="ab", label="repoAB", path="/src/repoAB", sessions=())
    assert added_workspace((A, sibling), "/src/repoAB/sub") == sibling
    assert added_workspace((A,), "/src/repoAB") is None


def test_the_new_workspace_containing_the_path_is_added_over_another_new_one() -> None:
    assert added_workspace((A, B), "/src/repoB/sub") == B


def test_a_workspace_without_a_path_contains_none() -> None:
    pathless = WorkspaceEntry(id="p", label="local", path=None, sessions=())
    assert added_workspace((pathless,), "/src/repoA") is None


def test_session_states_change_in_every_workspace() -> None:
    changed = SessionStateChanged(session="s", active=True, state="running", viewers=1)
    [a, b] = with_states((A, B), changed)
    assert a.sessions[1].state == "running" and a.sessions[0] == EARLIER
    assert b == B


def test_hints() -> None:
    assert picker_hint(WORKSPACES_HINTS, startup=False, box=None) == LIST_HINT
    assert picker_hint(WORKSPACES_HINTS, startup=True, box=None) == STARTUP_HINT
    assert PATH_HINT == "enter add · esc back"


def test_row_keys_tell_rows_apart_by_workspace_or_path() -> None:
    assert row_key(A) == row_key(A.model_copy(update={"sessions": ()})) == "a"
    assert row_key(AddWorkspace(path="/src/a")) == "add /src/a"
    assert row_key(ADD_PATH) == "add_path"
