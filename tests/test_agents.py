import itertools

import pytest

from orbit.agent.domain.agents import AgentRegistry


def registry() -> AgentRegistry:
    counter = itertools.count(1)
    return AgentRegistry(lambda: f"token-{next(counter)}")


def test_registering_assigns_numbered_ids_and_fresh_tokens() -> None:
    agents = registry()
    first = agents.register("coder", task_id="1")
    second = agents.register("reviewer", task_id="2")
    assert (first.id, first.role, first.task_id, first.token) == (
        "agent-1",
        "coder",
        "1",
        "token-1",
    )
    assert (second.id, second.token) == ("agent-2", "token-2")


def test_tokens_resolve_to_their_agent() -> None:
    agents = registry()
    agent = agents.register("coder", task_id="1")
    assert agents.by_token("token-1") == agent
    assert agents.by_token("token-9") is None


def test_revoked_tokens_no_longer_resolve_and_ids_are_not_reused() -> None:
    agents = registry()
    agents.register("coder", task_id="1")
    agents.revoke("agent-1")
    assert agents.by_token("token-1") is None
    assert agents.register("coder", task_id="1").id == "agent-2"


def test_revoking_twice_or_an_unknown_agent_is_harmless() -> None:
    agents = registry()
    agents.register("coder", task_id="1")
    agents.revoke("agent-1")
    agents.revoke("agent-1")
    agents.revoke("agent-7")
    assert agents.live == ()


def test_live_lists_unrevoked_agents_in_start_order() -> None:
    agents = registry()
    first = agents.register("coder", task_id="1")
    agents.register("reviewer", task_id="2")
    third = agents.register("reviewer", task_id="3")
    agents.revoke("agent-2")
    assert agents.live == (first, third)


def test_a_repeated_token_is_rejected() -> None:
    agents = AgentRegistry(lambda: "same")
    agents.register("coder", task_id="1")
    with pytest.raises(RuntimeError):
        agents.register("coder", task_id="2")
