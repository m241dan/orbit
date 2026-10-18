import pytest

from orbit.tui.view import NEIGHBORS, SIDEBAR_WINDOWS, neighbors


@pytest.mark.parametrize(
    ("selected", "moves"),
    [
        ("transcript", {"j": "prompt", "l": "task-list"}),
        ("detail", {"k": "transcript", "j": "prompt", "l": "task-list"}),
        ("prompt", {"k": "transcript", "l": "verification-steps"}),
        ("task-list", {"h": "transcript", "j": "verification-steps"}),
        ("verification-steps", {"h": "prompt", "k": "task-list", "j": "agents"}),
        ("agents", {"h": "prompt", "k": "verification-steps"}),
    ],
)
def test_each_window_reaches_its_neighbours(selected: str, moves: dict[str, str]) -> None:
    assert neighbors(selected, detail_open=False, sidebar_shown=True) == moves


def test_transcript_moves_down_to_the_open_detail_pane() -> None:
    moves = neighbors("transcript", detail_open=True, sidebar_shown=True)

    assert moves == {"j": "detail", "l": "task-list"}


@pytest.mark.parametrize("selected", ["detail", "prompt", "task-list", "agents"])
def test_open_detail_pane_changes_only_the_transcripts_moves(selected: str) -> None:
    assert neighbors(selected, detail_open=True, sidebar_shown=True) == NEIGHBORS[selected]


@pytest.mark.parametrize("selected", list(NEIGHBORS))
@pytest.mark.parametrize("detail_open", [False, True])
def test_hidden_sidebar_drops_sidebar_windows(selected: str, detail_open: bool) -> None:
    shown = neighbors(selected, detail_open, sidebar_shown=True)

    hidden = neighbors(selected, detail_open, sidebar_shown=False)

    assert hidden == {key: window for key, window in shown.items() if window not in SIDEBAR_WINDOWS}


def test_hidden_sidebar_keeps_the_transcripts_way_to_the_detail_pane() -> None:
    moves = neighbors("transcript", detail_open=True, sidebar_shown=False)

    assert moves == {"j": "detail"}


def test_hidden_sidebar_leaves_a_sidebar_window_only_its_way_out() -> None:
    moves = neighbors("verification-steps", detail_open=False, sidebar_shown=False)

    assert moves == {"h": "prompt"}
