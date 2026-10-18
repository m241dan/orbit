"""The conversation's turns: final answers, rule timings, and what an agent's exit draws."""

from orbit.protocol import (
    AgentExited,
    AgentStarted,
    AssistantMessage,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from orbit.tui.view.rules import DrawnRule
from orbit.tui.view.turns import (
    ExitedAgent,
    FinishedTurn,
    Turns,
    agent_exited,
    agent_started,
    turn_finished,
    turn_output,
    turn_started,
)


def started(agent: str | None, turn: str = "t") -> TurnStarted:
    return TurnStarted(turn_id=turn, agent_id=agent)


def said(agent: str | None, turn: str = "t") -> AssistantMessage:
    return AssistantMessage(turn_id=turn, agent_id=agent, text="hi")


def called(agent: str | None, turn: str = "t") -> ToolCallStarted:
    return ToolCallStarted(turn_id=turn, agent_id=agent, call_id="c", name="Bash", arguments={})


def ended(agent: str | None, turn: str = "t") -> TurnFinished:
    return TurnFinished(turn_id=turn, agent_id=agent, reason="completed")


def exited(agent: str) -> AgentExited:
    return AgentExited(agent_id=agent, reason="killed")


def test_a_turn_ending_on_a_message_after_a_tool_call_has_it_as_its_final_answer() -> None:
    turns = turn_started(Turns(), started("a"), 1)
    for event in (said("a"), called("a"), said("a")):
        turns = turn_output(turns, event)
    assert turn_finished(turns, ended("a"), 4)[1] == FinishedTurn((1,), 3)


def test_a_turn_ending_on_a_tool_call_has_no_final_answer() -> None:
    turns = turn_output(turn_output(Turns(), said("a")), called("a"))
    assert turn_finished(turns, ended("a"), 4)[1].answer == ()


def test_a_turn_not_seen_starting_has_no_elapsed_time() -> None:
    assert turn_finished(Turns(), ended("a"), 4)[1] == FinishedTurn((), None)


def test_a_finished_turn_is_forgotten_and_its_rule_drawn_for_its_agent() -> None:
    turns = turn_output(turn_started(Turns(), started("a"), 1), said("a"))
    after, _ = turn_finished(turns, ended("a"), 3)
    assert after == Turns(agent_since={"a": 1}, drawn={"a": DrawnRule(ended("a"), 2)})


def test_the_planners_finished_turn_draws_no_rule_to_replace() -> None:
    turns = turn_started(Turns(), started(None), 1)
    assert turn_finished(turns, ended(None), 3) == (Turns(), FinishedTurn((), 2))


def test_equal_turn_ids_of_different_agents_are_kept_apart() -> None:
    turns = turn_started(turn_started(Turns(), started("a"), 1), started("b"), 2)
    turns = turn_output(turn_output(turns, said("a")), called("b"))
    _, finished_a = turn_finished(turns, ended("a"), 5)
    _, finished_b = turn_finished(turns, ended("b"), 5)
    assert (finished_a, finished_b) == (FinishedTurn((0,), 4), FinishedTurn((), 3))


def test_an_agent_starting_a_turn_clears_its_drawn_rule() -> None:
    turns, _ = turn_finished(turn_started(Turns(), started("a"), 1), ended("a"), 2)
    turns = turn_started(turns, started("a", "u"), 5)
    assert turns.drawn == {}
    assert turns.agent_since == {"a": 5}


def test_an_exit_after_a_turn_ended_replaces_that_turns_rule() -> None:
    turns, _ = turn_finished(turn_started(Turns(), started("a"), 1), ended("a"), 3)
    after, exit_ = agent_exited(turns, exited("a"), 10)
    assert exit_ == ExitedAgent((), DrawnRule(ended("a"), 2), 9)
    assert after == Turns()


def test_an_exit_with_no_drawn_rule_is_timed_from_the_agents_start() -> None:
    turns = agent_started(Turns(), AgentStarted(agent_id="a", role="coder", task_id="1"), 2)
    assert agent_exited(turns, exited("a"), 7)[1] == ExitedAgent((), None, 5)


def test_an_exit_with_no_start_seen_has_no_elapsed_time() -> None:
    assert agent_exited(Turns(), exited("a"), 7)[1] == ExitedAgent((), None, None)


def test_an_exit_mid_turn_gives_the_final_answers_of_only_its_agents_open_turns() -> None:
    turns = turn_output(turn_output(Turns(), said("a", "t")), said("b", "t"))
    turns = turn_output(turn_output(turns, said("a", "u")), called("a", "u"))
    after, exit_ = agent_exited(turns, exited("a"), 7)
    assert exit_.answers == (("t", (0,)), ("u", ()))
    assert after.steps == {("b", "t"): ("message",)}


def test_an_exit_mid_turn_leaves_no_state_for_its_agent() -> None:
    turns = turn_started(turn_started(Turns(), started("a"), 1), started("b"), 2)
    turns = turn_output(turn_output(turns, said("a")), said("b"))
    after, _ = agent_exited(turns, exited("a"), 7)
    assert after == Turns(
        steps={("b", "t"): ("message",)}, started={("b", "t"): 2}, agent_since={"b": 2}
    )
