from typing import get_args

import pytest

from orbit.protocol import (
    TaskStatus,
)
from orbit.tui.view.common import STATUS_ICONS, counted, text_lines, token_text


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
