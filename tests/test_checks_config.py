"""Checks an agent proposes, read and validated as the config would read them."""

import re

import pytest

from orbit.agent.domain.checks import ChecksProposal, Setup, VerificationSet, parse_proposal
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.triggers import ChecksTrigger, Trigger
from orbit.agent.domain.verification import VerificationStep


def test_a_proposal_reads_like_the_config() -> None:
    proposal = parse_proposal(
        {
            "steps": [{"name": "Lint", "command": ["ruff", "check"]}],
            "setup": {"command": ["uv", "sync"], "watch": ["uv.lock"]},
        }
    )
    assert proposal == ChecksProposal(
        steps=(VerificationStep("Lint", ("ruff", "check")),),
        setup=Setup(command=("uv", "sync"), watch=("uv.lock",)),
    )
    assert parse_proposal({"steps": [{"name": "T", "command": ["t"]}], "setup": None}).setup is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "steps"),
        ({"steps": []}, "steps"),
        ({"steps": [{"name": "", "command": ["x"]}]}, "step 1 has no name"),
        ({"steps": [{"name": "a", "command": []}]}, "step 1 has no command"),
        ({"steps": [{"name": "a", "command": ["x"]}], "setup": {"command": []}}, "setup.command"),
        ({"steps": [{"name": "a", "command": ["x"]}], "extra": 1}, "extra"),
    ],
)
def test_invalid_proposals_are_reported_readably(data: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_proposal(data)


SET_PROPOSAL: dict[str, object] = {
    "steps": [
        {"name": "A", "command": ["a"]},
        {"name": "B", "command": ["b"]},
        {"name": "C", "command": ["c"], "set": "mutation"},
    ],
    "sets": {"mutation": False},
    "triggers": {"mutation-every-10": {"on": "commits", "every": 10, "checks": "mutation"}},
}


def test_a_proposal_reads_its_sets_holding_merges_and_its_triggers() -> None:
    proposal = parse_proposal(SET_PROPOSAL)
    assert proposal.steps[2] == VerificationStep("C", ("c",), set="mutation")
    assert proposal.sets == {"mutation": VerificationSet(holds_merges=False)}
    assert proposal.triggers == {
        "mutation-every-10": ChecksTrigger(on="commits", every=10, checks="mutation")
    }


def test_a_proposal_without_sets_or_triggers_has_none() -> None:
    proposal = parse_proposal({"steps": [{"name": "T", "command": ["t"]}]})
    assert proposal.sets == {}
    assert proposal.triggers == {}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"triggers": {"slow": {"on": "commits", "checks": "stress"}}},
            'triggers.slow.checks: stress isn\'t "task-checks" or a set a step is in; the sets'
            " are mutation.",
        ),
        ({"sets": {"stress": True}}, "verification.sets.stress: no step is in it"),
        ({"sets": {"task-checks": True}}, "task-checks names the steps in no set"),
        ({"sets": {"mutation": "yes"}}, "sets.mutation"),
        ({"sets": ["mutation"]}, "sets"),
        (
            {"triggers": {"slow": {"on": "task_landed", "every": 3, "checks": "mutation"}}},
            'triggers.slow.every: every = 3 needs on = "commits".',
        ),
        (
            {"triggers": {"slow": {"on": "manual", "manual": True, "checks": "mutation"}}},
            "triggers.slow.label: manual = true needs a label.",
        ),
        ({"triggers": {"slow": {"on": "never", "checks": "mutation"}}}, "never isn't an event"),
        ({"triggers": {"slow": {"on": "commits"}}}, "triggers.slow.checks"),
        (
            {"triggers": {"slow": {"on": "commits", "checks": "mutation", "role": "coder"}}},
            "triggers.slow.role",
        ),
        ({"triggers": {"a b": {"on": "commits", "checks": "mutation"}}}, "a trigger's name"),
    ],
)
def test_invalid_sets_and_triggers_are_refused_as_the_config_would(
    change: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        parse_proposal(SET_PROPOSAL | change)


def test_a_trigger_may_run_the_task_checks() -> None:
    trigger = {"on": "manual", "manual": True, "label": "Check", "checks": "task-checks"}
    proposal = parse_proposal(SET_PROPOSAL | {"triggers": {"check": trigger}})
    assert proposal.triggers["check"].checks == "task-checks"


BUILT_IN: dict[str, Trigger] = {
    "review": Trigger(on="task_landed", role="reviewer", prompt="Review {commit}."),
    "startup-check": Trigger(
        on="session_started", checks="task-checks", when={"verification": "configured"}
    ),
}
"""Built-in triggers, a role's and a run of the task checks, as the package might ship them."""


def test_a_proposed_trigger_named_like_a_built_in_role_trigger_is_refused() -> None:
    trigger = {"on": "commits", "every": 10, "checks": "mutation"}
    with pytest.raises(ConfigError) as refused:
        parse_proposal(SET_PROPOSAL | {"triggers": {"review": trigger}}, BUILT_IN)
    assert str(refused.value) == (
        "triggers.review.checks: review is taken by a built-in trigger, which queues a"
        " reviewer's task; give this trigger another name."
    )


def test_a_proposed_trigger_may_override_a_built_in_checks_trigger() -> None:
    trigger = {"on": "session_started", "checks": "task-checks"}
    proposal = parse_proposal(SET_PROPOSAL | {"triggers": {"startup-check": trigger}}, BUILT_IN)
    assert proposal.triggers["startup-check"] == ChecksTrigger(
        on="session_started", checks="task-checks"
    )


def test_a_proposed_trigger_is_checked_merged_over_the_built_in_of_its_name() -> None:
    trigger = {"on": "commits", "checks": "mutation"}
    with pytest.raises(ConfigError, match=re.escape("triggers.startup-check.when")):
        parse_proposal(SET_PROPOSAL | {"triggers": {"startup-check": trigger}}, BUILT_IN)
