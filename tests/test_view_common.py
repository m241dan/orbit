from typing import get_args

import pytest

from orbit.protocol import (
    TaskStatus,
)
from orbit.tui.view import (
    STATUS_ICONS,
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
