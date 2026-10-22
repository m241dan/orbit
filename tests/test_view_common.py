from typing import get_args

import pytest

from orbit.protocol import (
    SessionSummary,
    TaskStatus,
)
from orbit.tui.view.common import (
    STATUS_ICONS,
    ListHints,
    active_session,
    counted,
    kept_highlight,
    picker_hint,
    text_lines,
    token_text,
)


def test_every_task_status_has_an_icon_except_running_which_spins() -> None:
    statuses = set(get_args(TaskStatus))
    assert statuses - set(STATUS_ICONS) == {"running"}
    assert STATUS_ICONS["gave_up"] == "🏳"
    assert STATUS_ICONS["draft"] == "📝"


@pytest.mark.parametrize(
    ("count", "text"),
    [
        (0, "0"),
        (812, "812"),
        (999, "999"),
        (1000, "1k"),
        (1250, "1.2k"),
        (8000, "8k"),
        (9999, "9.9k"),
        (10000, "10k"),
        (14_200, "14k"),
        (999_999, "999k"),
        (1_000_000, "1M"),
        (1_250_000, "1.2M"),
        (12_000_000, "12M"),
    ],
)
def test_token_counts_are_compact_and_rounded_down(count: int, text: str) -> None:
    assert token_text(count) == text


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        ("", []),
        ("one", ["one"]),
        ("one\n", ["one"]),
        ("one\n\ntwo", ["one", "", "two"]),
        ("a\x0cb\x1cc\x85d\u2028e", ["a\x0cb\x1cc\x85d\u2028e"]),
        ("one\r\ntwo\r\n", ["one", "two"]),
    ],
)
def test_text_lines_split_only_at_newlines(text: str, lines: list[str]) -> None:
    assert text_lines(text) == lines


@pytest.mark.parametrize(("count", "text"), [(0, "0 lines"), (1, "1 line"), (2, "2 lines")])
def test_counted_is_singular_only_for_one(count: int, text: str) -> None:
    assert counted(count, "line") == text


def test_counted_takes_an_irregular_plural() -> None:
    assert (counted(1, "match", "matches"), counted(3, "match", "matches")) == (
        "1 match",
        "3 matches",
    )


SHOWN = ("a", "b", "c", None)


@pytest.mark.parametrize(
    ("keys", "highlight"),
    [
        (("c", "b", "a", None), 1),  # Reordered: it follows its key.
        (("x", "a", "b", "c", None), 2),  # One appeared above it.
        (("a", "c", None), 1),  # Its own row went: the nearest one.
        (("a", None), 1),  # And kept within the rows.
    ],
)
def test_the_highlight_follows_its_key_or_goes_to_the_nearest_row(
    keys: tuple[str | None, ...], highlight: int
) -> None:
    assert kept_highlight(SHOWN, 1, keys) == highlight


def test_a_highlighted_trailing_row_stays_highlighted() -> None:
    assert kept_highlight(SHOWN, 3, ("x", "a", None)) == 2


def test_a_highlighted_trailing_row_that_went_leaves_nothing_highlighted() -> None:
    assert kept_highlight(SHOWN, 3, ("a", "b")) is None


def test_nothing_highlighted_stays_so() -> None:
    assert kept_highlight(SHOWN, None, ("a", "b", None)) is None


def test_a_gone_last_row_moves_the_highlight_onto_the_trailing_action_row() -> None:
    assert kept_highlight(SHOWN, 2, ("a", "b", None)) == 2


def test_no_rows_left_leave_nothing_highlighted() -> None:
    assert kept_highlight(SHOWN, 1, ()) is None


def test_rows_listed_after_none_leave_nothing_highlighted() -> None:
    assert kept_highlight((), None, ("a", "b", None)) is None


HINTS = ListHints(closing="enter open · esc close", quitting="enter open · esc quit")


def test_a_picker_shows_its_lists_keys_where_escape_closes_it() -> None:
    assert picker_hint(HINTS, startup=False, box=None) == "enter open · esc close"


def test_at_startup_a_picker_shows_escape_quits() -> None:
    assert picker_hint(HINTS, startup=True, box=None) == "enter open · esc quit"


def test_a_picker_shows_its_text_boxs_keys_while_typing() -> None:
    assert picker_hint(HINTS, startup=True, box="enter add · esc back") == "enter add · esc back"


def test_with_no_active_session_none_is_active() -> None:
    assert active_session((SessionSummary(id="a", name="a"),)) is None


def test_the_one_active_session_is_the_active_one() -> None:
    active = SessionSummary(id="b", name="b", active=True)
    assert active_session((SessionSummary(id="a", name="a"), active)) == active
