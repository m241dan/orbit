"""The rules ending turns and agents' runs, and a turn's final answer."""

import pytest

from orbit.protocol import AgentExited, AgentExitReason, TurnEndReason, TurnFinished, Usage
from orbit.tui.view import (
    DrawnRule,
    EntryKind,
    ExitRule,
    duration_text,
    exit_rule,
    final_answer,
    turn_rule_entry,
    usage_text,
)


def ended(reason: TurnEndReason, usage: Usage | None = None) -> TurnFinished:
    return TurnFinished(turn_id="t", agent_id="agent-3", reason=reason, usage=usage)


@pytest.mark.parametrize(
    ("reason", "label", "kind"),
    [
        ("completed", "done", "rule"),
        ("cancelled", "stopped", "rule"),
        ("max_steps", "out of steps", "rule"),
        ("error", "error", "error_rule"),
    ],
)
def test_a_turns_rule_says_how_it_ended_in_its_kind(
    reason: TurnEndReason, label: str, kind: EntryKind
) -> None:
    assert turn_rule_entry(ended(reason), None, None) == (label, kind)


def test_a_turns_rule_is_headed_by_its_speaker() -> None:
    assert turn_rule_entry(ended("completed"), 250.0, "agent-3") == ("agent-3 done · 4m10s", "rule")


def test_a_turns_rule_without_usage_shows_the_time_elapsed() -> None:
    assert turn_rule_entry(ended("cancelled"), 42.7, None) == ("stopped · 42s", "rule")


def test_a_turns_rule_with_usage_shows_its_time_and_tokens() -> None:
    usage = Usage(
        input_tokens=200_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=50_000,
        output_tokens=8_400,
        duration_ms=72_500,
    )
    assert turn_rule_entry(ended("completed", usage), 5.0, None) == (
        "done · 1m12s · ↑1.2M ↓8.4k",
        "rule",
    )


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (59.9, "59s"),
        (60, "1m00s"),
        (72, "1m12s"),
        (3599, "59m59s"),
        (3600, "1h00m"),
        (3780, "1h03m"),
    ],
)
def test_durations_read_in_seconds_minutes_or_hours(seconds: float, text: str) -> None:
    assert duration_text(seconds) == text


def test_usage_reads_input_with_the_cache_up_and_output_down() -> None:
    usage = Usage(
        input_tokens=10, cache_creation_input_tokens=20, cache_read_input_tokens=30, output_tokens=5
    )
    assert usage_text(usage) == "↑60 ↓5"


def test_only_the_message_after_the_last_tool_call_is_the_final_answer() -> None:
    assert final_answer(["message", "tool", "message"]) == (1,)


def test_a_turn_of_a_message_alone_has_it_as_its_final_answer() -> None:
    assert final_answer(["message"]) == (0,)


def test_a_turn_ending_on_a_tool_call_has_no_final_answer() -> None:
    assert final_answer(["message", "tool"]) == ()


def test_every_message_after_the_last_tool_call_is_the_final_answer() -> None:
    assert final_answer(["tool", "message", "tool", "message", "message"]) == (1, 2)


def test_a_turn_with_no_output_has_no_final_answer() -> None:
    assert final_answer([]) == ()


def exited(reason: AgentExitReason) -> AgentExited:
    return AgentExited(agent_id="agent-3", reason=reason)


@pytest.mark.parametrize(
    ("reason", "text", "kind"),
    [
        ("finished", "agent-3 done · 42s", "rule"),
        ("killed", "agent-3 stopped · 42s", "rule"),
        ("crashed", "agent-3 crashed · 42s", "error_rule"),
    ],
)
def test_an_agent_exiting_without_a_turn_rule_adds_its_own(
    reason: AgentExitReason, text: str, kind: EntryKind
) -> None:
    assert exit_rule(exited(reason), None, 42.5, "agent-3") == ExitRule(text, kind, False)


def test_an_agents_own_exit_rule_shows_no_time_when_unknown() -> None:
    assert exit_rule(exited("crashed"), None, None, None) == ExitRule(
        "crashed", "error_rule", False
    )


def test_an_agent_finishing_after_its_turn_rule_adds_nothing() -> None:
    assert exit_rule(exited("finished"), DrawnRule(ended("completed"), 5.0), 9.0, "agent-3") is None


@pytest.mark.parametrize("reason", ["cancelled", "max_steps", "error"])
def test_an_agent_killed_after_a_rule_not_done_leaves_it(reason: TurnEndReason) -> None:
    assert exit_rule(exited("killed"), DrawnRule(ended(reason), 5.0), 9.0, "agent-3") is None


def test_an_agent_killed_after_a_done_rule_turns_it_stopped_keeping_its_cost() -> None:
    usage = Usage(input_tokens=210_000, output_tokens=12_000, duration_ms=250_000)
    drawn = DrawnRule(ended("completed", usage), 5.0)
    assert exit_rule(exited("killed"), drawn, 9.0, "agent-3") == ExitRule(
        "agent-3 stopped · 4m10s · ↑210k ↓12k", "rule", True
    )


def test_an_agent_crashing_after_a_done_rule_turns_it_a_crashed_error_rule() -> None:
    drawn = DrawnRule(ended("completed"), 42.0)
    assert exit_rule(exited("crashed"), drawn, 9.0, None) == ExitRule(
        "crashed · 42s", "error_rule", True
    )
