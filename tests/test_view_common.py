from typing import get_args

from orbit.protocol import (
    TaskStatus,
)
from orbit.tui.view import (
    STATUS_ICONS,
)


def test_every_task_status_has_an_icon_except_running_which_spins() -> None:
    statuses = set(get_args(TaskStatus))
    assert statuses - set(STATUS_ICONS) == {"running"}
    assert STATUS_ICONS["gave_up"] == "🏳"
    assert STATUS_ICONS["draft"] == "📝"
