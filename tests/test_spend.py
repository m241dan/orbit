"""What the session spent, by window: a conversational role's corner, the session's, the
breakdown by role, and the corner following the tab shown and the session's spend."""

from orbit.protocol import SessionStateChanged, Usage
from orbit.tui.view.spend import (
    NO_SPENDING,
    UsageCorner,
    role_corner,
    session_corner,
    spending_text,
)
from tests.tui_fakes import SNAPSHOT

ROLE_SPENT = {"planner": Usage(cost_usd=0.42), "experimenter": Usage(cost_usd=1.10)}
CAUSED_ONE = {"planner": {"experimenter": Usage(cost_usd=1.10)}}
CAUSED_THREE = {
    "planner": {
        "experimenter": Usage(cost_usd=1.10),
        "architect": Usage(cost_usd=0.30),
        "tester": Usage(cost_usd=0.30),
    }
}


class Corner:
    def __init__(self) -> None:
        self.text = ""

    def show_corner(self, text: str) -> None:
        self.text = text


def test_a_role_that_caused_spend_names_its_consultant_and_the_sum() -> None:
    corner = role_corner("planner", ROLE_SPENT, CAUSED_ONE)
    assert corner == "planner $0.42 · via experimenter $1.10 · $1.52"


def test_a_role_that_caused_nothing_shows_just_its_own_figure() -> None:
    assert role_corner("experimenter", ROLE_SPENT, CAUSED_ONE) == "$1.10"


def test_two_consultants_are_named_and_more_are_counted() -> None:
    two = {"planner": dict(list(CAUSED_THREE["planner"].items())[:2])}
    assert role_corner("planner", ROLE_SPENT, two) == (
        "planner $0.42 · via experimenter $1.10, architect $0.30 · $1.82"
    )
    assert role_corner("planner", ROLE_SPENT, CAUSED_THREE) == (
        "planner $0.42 · via 3 consultants $1.70 · $2.12"
    )


def test_a_role_with_credits_only_shows_its_credits() -> None:
    assert role_corner("kiro", {"kiro": Usage(credits=0.62)}, {}) == "0.62 credits"


def test_a_role_that_spent_nothing_itself_shows_no_cost_beside_what_it_caused() -> None:
    assert role_corner("planner", {}, CAUSED_ONE) == (
        "planner $0.00 · via experimenter $1.10 · $1.10"
    )


def test_the_session_corner_shows_the_total_or_nothing() -> None:
    assert session_corner(Usage(cost_usd=2.0)) == "session $2.00"
    assert session_corner(Usage()) == ""


def test_the_breakdown_lists_each_roles_own_spend_and_what_it_caused() -> None:
    assert spending_text(ROLE_SPENT, CAUSED_ONE) == (
        "planner: $0.42 itself\n  caused experimenter: $1.10\nexperimenter: $1.10 itself"
    )
    assert spending_text({}, {}) == NO_SPENDING


def corner_over(task_usage: Usage | None = None) -> tuple[UsageCorner, Corner]:
    view = Corner()
    corner = UsageCorner(view, lambda: Usage(cost_usd=3.0), lambda agent: task_usage)
    return corner, view


def test_the_corner_follows_the_viewed_sessions_spend_by_role() -> None:
    corner, view = corner_over()
    corner.show_tab("planner", True)
    corner.track(SNAPSHOT.model_copy(update={"role_spent": ROLE_SPENT}))
    assert view.text == "$0.42"
    other = SessionStateChanged(
        session="other", active=False, state="idle", viewers=0, role_spent={}
    )
    corner.track(other)
    assert view.text == "$0.42"
    changed = other.model_copy(
        update={"session": "s1", "role_spent": ROLE_SPENT, "caused_spent": CAUSED_ONE}
    )
    corner.track(changed)
    assert view.text == "planner $0.42 · via experimenter $1.10 · $1.52"
    assert corner.breakdown() == spending_text(ROLE_SPENT, CAUSED_ONE)


def test_the_corner_shows_the_session_on_all_and_a_task_agents_task_usage() -> None:
    corner, view = corner_over(Usage(credits=0.5))
    corner.show_tab(None, False)
    assert view.text == "session $3.00"
    assert not corner.conversational
    corner.show_tab("agent-9", False)
    assert view.text == "0.50 credits"


def test_the_corner_shows_nothing_for_an_agent_with_no_task_on_the_list() -> None:
    corner, view = corner_over()
    corner.show_tab("coder", False)
    assert view.text == ""
