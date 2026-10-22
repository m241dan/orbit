"""The workspace picker's rows, labels and hints."""

import pytest

from orbit.protocol import (
    AddWorkspace,
    RemoveWorkspace,
    SessionStateChanged,
    SessionSummary,
    WorkspaceCloning,
    WorkspaceEntry,
)
from orbit.tui.view.common import kept_highlight, picker_hint
from orbit.tui.view.workspaces import (
    ADD_PATH,
    LIST_HINT,
    PATH_HINT,
    REMOVE_MESSAGE,
    STARTUP_HINT,
    WORKSPACES_HINTS,
    add_command,
    added_workspace,
    clone_failed,
    clone_label,
    clone_name,
    clones_left,
    preselected,
    removal,
    remove_choices,
    remove_title,
    row_key,
    row_label,
    with_clone,
    with_states,
    workspace_at,
    workspace_label,
    workspace_rows,
)

TODAY = SessionSummary(id="s", name="today", active=True)
EARLIER = SessionSummary(id="e", name="earlier")
A = WorkspaceEntry(id="a", label="repoA", path="/src/repoA", sessions=(EARLIER, TODAY))
B = WorkspaceEntry(id="b", label="repoB", path="/src/repoB", sessions=())
CLONE = WorkspaceEntry(id="c", label="proj", path="/srv/clones/proj", managed=True, sessions=())
CLONING = WorkspaceCloning(url="https://github.com/o/proj.git", phase="Receiving objects")


def test_rows_list_the_served_workspaces_then_adding_a_path() -> None:
    assert workspace_rows((A, B), "/src/repoA") == (A, B, ADD_PATH)


def test_rows_list_the_clones_under_way_before_adding_a_path() -> None:
    assert workspace_rows((A,), "/src/repoA", (CLONING,)) == (A, CLONING, ADD_PATH)


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
    assert row_label(ADD_PATH) == "Add a path or URL…"
    assert row_label(CLONING) == "Cloning proj… Receiving objects"


def test_a_clones_row_is_keyed_by_its_url() -> None:
    assert row_key(CLONING) == "cloning https://github.com/o/proj.git"


@pytest.mark.parametrize(
    "text", ["https://github.com/o/r", "git@github.com:o/r.git", "file:///x/r.git", "ssh://h/r"]
)
def test_text_that_looks_like_a_url_is_added_as_one(text: str) -> None:
    assert add_command(text) == AddWorkspace(url=text)


@pytest.mark.parametrize("text", ["/srv/repo", "~/repo", "/srv/a@b:c", "repo:2"])
def test_other_text_is_added_as_a_path(text: str) -> None:
    assert add_command(text) == AddWorkspace(path=text)


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://github.com/o/proj.git", "proj"),
        ("git@github.com:proj", "proj"),
        ("file:///x/my proj/", "my-proj"),
        ("https://host/", "host"),
        ("https://host/.git", "repository"),
    ],
)
def test_a_clone_is_named_as_the_server_names_its_directory(url: str, name: str) -> None:
    assert clone_name(url) == name


def test_a_clone_shows_how_far_through_its_phase_it_is() -> None:
    progress = CLONING.model_copy(update={"percent": 42})
    assert clone_label(progress) == "Cloning proj… 42%"


def test_a_clones_progress_takes_the_place_of_its_last() -> None:
    other = WorkspaceCloning(url="git@host:o/other.git", phase="Counting objects")
    progress = CLONING.model_copy(update={"percent": 42})
    assert with_clone((CLONING, other), progress) == (progress, other)


def test_a_new_clones_progress_goes_after_the_others() -> None:
    other = WorkspaceCloning(url="git@host:o/other.git", phase="Counting objects")
    assert with_clone((CLONING,), other) == (CLONING, other)


def test_a_clone_is_done_once_a_new_workspace_with_its_name_is_listed() -> None:
    other = WorkspaceCloning(url="git@host:o/other.git", phase="Counting objects")
    assert clones_left((CLONING, other), (A,), (A, CLONE)) == (other,)


def test_a_clone_numbered_beside_one_of_its_name_is_done_once_listed() -> None:
    numbered = CLONE.model_copy(update={"id": "d", "label": "proj-2"})
    assert clones_left((CLONING,), (CLONE,), (CLONE, numbered)) == ()


def test_a_clone_is_under_way_while_only_an_old_workspace_has_its_name() -> None:
    other = A.model_copy(update={"id": "e", "label": "projector"})
    assert clones_left((CLONING,), (CLONE,), (CLONE, other)) == (CLONING,)


def test_a_failure_belongs_to_the_clone_of_the_url_it_names() -> None:
    url = "https://github.com/o/proj"
    assert clone_failed(f"Couldn't clone {url}: not found", url)
    assert not clone_failed(f"Couldn't clone {url}2: not found", url)


def test_removing_asks_about_the_workspace_by_its_label() -> None:
    assert remove_title(A) == "Stop serving repoA"
    assert REMOVE_MESSAGE.startswith("Its sessions can be kept")


def test_removing_offers_to_keep_or_delete_its_sessions_or_cancel() -> None:
    assert remove_choices(A) == ("Keep its sessions", "Delete its sessions too", "Cancel")


def test_removing_a_clone_names_the_directory_deleting_it_deletes() -> None:
    assert remove_choices(CLONE)[1] == (
        "Delete its sessions too, and its clone directory /srv/clones/proj"
    )


def test_each_choice_removes_keeping_or_deleting_its_sessions_or_nothing() -> None:
    assert removal(A, 0) == RemoveWorkspace(workspace="a")
    assert removal(A, 1) == RemoveWorkspace(workspace="a", purge=True)
    assert removal(A, 2) is None


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


def test_dot_dot_in_the_path_leads_to_the_workspace_it_names() -> None:
    c = WorkspaceEntry(id="c", label="repoC", path="/src/repoC", sessions=())
    assert added_workspace((A, c), "/src/repoA/../repoC") == c
    assert added_workspace((A,), "/src/repoC/../repoA/sub") == A


def test_dot_in_the_path_is_ignored() -> None:
    assert added_workspace((B, A), "/src/./repoA/sub") == A
    assert added_workspace((B, A), "/src/repoA/./sub") == A


def test_dot_dot_out_of_a_workspace_served_does_not_match_it() -> None:
    assert added_workspace((A, B), "/src/repoA/../repoB") == B
    assert added_workspace((A,), "/src/repoA/../repoB") is None


def test_duplicate_slashes_in_the_path_are_collapsed() -> None:
    assert added_workspace((B, A), "/src//repoA") == A


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


def test_relisting_with_no_row_highlighted_highlights_none() -> None:
    keys = tuple(map(row_key, workspace_rows((A, B), None)))
    assert kept_highlight(keys, None, keys) is None


def test_relisting_without_the_highlighted_workspace_keeps_it_on_the_nearest_row() -> None:
    shown = tuple(map(row_key, workspace_rows((A, B), None)))
    keys = tuple(map(row_key, workspace_rows((A,), None)))
    assert kept_highlight(shown, 1, keys) == 1 and keys[1] == row_key(ADD_PATH)
