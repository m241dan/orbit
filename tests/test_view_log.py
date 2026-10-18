"""A log's rows: which entries are shown, the cursor's nearest row, thinking and rules."""

from orbit.tui.view.log import (
    Entry,
    after_rows,
    latest_rule,
    nearest_row,
    shown,
    shown_rows,
    thinking_places,
)

THOUGHT = Entry("hmm", "agent", "coder", turn="t1")
SAID = Entry("done", "agent", "coder")


def test_shown_leaves_out_thinking_only_when_hidden() -> None:
    assert shown(THOUGHT, thinking_hidden=False)
    assert not shown(THOUGHT, thinking_hidden=True)
    assert shown(SAID, thinking_hidden=True)


def test_shown_rows_holds_every_entry_while_thinking_shows() -> None:
    assert shown_rows([SAID, THOUGHT, SAID], thinking_hidden=False) == [0, 1, 2]


def test_shown_rows_leaves_out_thinking_while_hidden() -> None:
    assert shown_rows([SAID, THOUGHT, SAID], thinking_hidden=True) == [0, 2]


def test_nearest_row_is_the_entry_itself_when_shown() -> None:
    assert nearest_row([0, 2, 5], 2) == 1


def test_nearest_row_picks_the_shown_entry_before() -> None:
    assert nearest_row([0, 2, 5], 4) == 1


def test_nearest_row_picks_the_shown_entry_after_when_none_is_before() -> None:
    assert nearest_row([3, 5], 1) == 0


def test_nearest_row_is_none_when_nothing_is_shown() -> None:
    assert nearest_row([], 1) is None


def test_after_rows_holds_when_nothing_is_shown() -> None:
    assert after_rows([], [0, 1])


def test_after_rows_holds_for_entries_after_the_last_row() -> None:
    assert after_rows([0, 2], [3, 4])


def test_after_rows_fails_for_an_entry_before_the_last_row() -> None:
    assert not after_rows([0, 2], [3, 1])


def test_thinking_places_match_both_agent_and_turn() -> None:
    entries = [
        THOUGHT,
        Entry("other turn", "agent", "coder", turn="t2"),
        Entry("other agent", "agent", "planner", turn="t1"),
        SAID,
        THOUGHT,
    ]
    assert thinking_places(entries, "coder", "t1") == [0, 4]


def test_latest_rule_is_the_agents_last_ruled_entry() -> None:
    entries = [
        Entry("first", "rule", "coder"),
        Entry("mine", "rule", "coder"),
        Entry("theirs", "rule", "planner"),
        SAID,
    ]
    assert latest_rule(entries, "coder") == 1


def test_latest_rule_is_none_without_one_of_the_agents() -> None:
    assert latest_rule([SAID, Entry("theirs", "rule", "planner")], "coder") is None
