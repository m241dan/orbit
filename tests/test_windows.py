"""Moving between the app's windows, without Textual: which window h/j/k/l select, what enter and
i do from each, and what the conversation's detail pane and the sidebar change."""

import pytest

from tests.frontend_fakes import Shell


def moved(*directions: str) -> Shell:
    """A shell whose user pressed ``directions``, with nothing focused."""
    shell = Shell()
    for direction in directions:
        shell.frontend.move(direction)
    return shell


def test_the_prompt_is_selected_to_begin_with() -> None:
    shell = Shell()
    assert (shell.app.selected, shell.frontend.windows.selected) == ({"prompt"}, "prompt")


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("k", "transcript"),
        ("l", "verification-steps"),
        ("kl", "task-list"),
        ("klj", "verification-steps"),
        ("kljh", "prompt"),
        ("klh", "transcript"),
        ("kj", "prompt"),
        ("lk", "task-list"),
        # Edges: nothing further in that direction.
        ("j", "prompt"),
        ("h", "prompt"),
        ("kk", "transcript"),
        ("kh", "transcript"),
        ("kll", "task-list"),
        ("lj", "agents"),
        ("ljk", "verification-steps"),
        ("ljh", "prompt"),
        ("ljj", "agents"),
        ("ljl", "agents"),
    ],
)
def test_hjkl_move_the_selection_between_windows(keys: str, expected: str) -> None:
    shell = moved(*keys)
    assert shell.app.selected == {expected}


def test_hjkl_move_nothing_while_a_window_has_the_focus() -> None:
    shell = Shell()
    shell.app.focused = "task-list"
    shell.frontend.move("k")
    assert shell.app.selected == {"prompt"}


def test_the_detail_pane_open_sits_between_the_conversation_and_the_prompt() -> None:
    shell = moved("k")
    shell.window.pane.open = True
    shell.frontend.move("j")
    assert shell.app.selected == {"detail"}


def test_the_sidebar_hidden_is_skipped() -> None:
    shell = Shell()
    shell.app.sidebar_shown = False
    shell.frontend.move("l")
    assert shell.app.selected == {"prompt"}


@pytest.mark.parametrize("keys", ["k", "kl", "l", "lk"])
def test_i_jumps_to_the_prompt_from_any_window_and_types(keys: str) -> None:
    shell = moved(*keys)
    shell.frontend.insert_mode()
    assert shell.app.selected == {"prompt"}
    assert shell.prompt.typing


def test_i_does_nothing_while_a_window_has_the_focus() -> None:
    shell = moved("k")
    shell.app.focused = "transcript"
    shell.frontend.insert_mode()
    assert (shell.app.selected, shell.prompt.typing) == ({"transcript"}, False)


@pytest.mark.parametrize("keys", ["k", "kl", "lj", "l"])
def test_enter_enters_the_selected_window(keys: str) -> None:
    shell = moved(*keys)
    shell.frontend.activate()
    assert shell.app.entered == [shell.frontend.windows.selected]


async def test_enter_on_the_prompt_of_a_live_session_does_nothing() -> None:
    shell = Shell()
    shell.frontend.activate()
    await shell.settle()
    assert (shell.app.entered, shell.wire.commands, shell.modals.offers) == ([], [], [])


def test_the_detail_pane_taking_the_focus_selects_it() -> None:
    shell = Shell()
    shell.frontend.detail_focused()
    assert shell.app.selected == {"detail"}


def test_escape_on_the_detail_pane_closes_it_and_selects_the_conversation() -> None:
    shell = Shell()
    shell.window.pane.open = True
    shell.frontend.detail_focused()
    shell.frontend.release_cursor()
    assert shell.app.selected == {"transcript"}
    assert not shell.window.pane.open


def test_escape_on_the_task_list_lets_go_of_it() -> None:
    shell = moved("k", "l")
    shell.tasks.hold()
    shell.frontend.release_cursor()
    assert shell.tasks.following


def test_hiding_the_sidebar_moves_a_sidebar_selection_to_the_conversation() -> None:
    shell = moved("l")
    shell.app.focused = "verification-steps"
    shell.frontend.sidebar_toggled(False)
    assert shell.app.selected == {"transcript"}
    assert shell.app.nothing_focused
    assert shell.app.unfocused == 1


def test_the_sidebar_shown_or_hidden_from_elsewhere_leaves_the_selection() -> None:
    shell = moved("k")
    shell.frontend.sidebar_toggled(False)
    shell.frontend.sidebar_toggled(True)
    assert shell.app.selected == {"transcript"}
    assert shell.app.unfocused == 0
