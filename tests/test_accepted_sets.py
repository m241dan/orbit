"""Accepted sets and triggers: merged over those configured, as the config they're written into
reads them, and used from the next task on: told to the next configurator, the trigger running
its set at its commit count, and a set come to hold merges holding them as it fails."""

import asyncio
from collections.abc import Callable

from orbit.agent import Verification
from orbit.agent.application.ports import ProposalDecision
from orbit.agent.application.proposals import proposed
from orbit.agent.domain.checks import (
    ChecksProposal,
    merge_proposed_sets,
    merge_proposed_triggers,
)
from orbit.agent.domain.triggers import Trigger
from orbit.agent.domain.verification import CheckSet, VerificationStep
from orbit.protocol import (
    CONFIGURE_CHECKS,
    TASK_CHECKS,
    AgentStarted,
    Event,
    FireTrigger,
    TasksChanged,
)
from tests.agent_fakes import (
    LINT,
    TRIGGERS,
    Configurator,
    FakeTree,
    Script,
    Steps,
    claims_done,
    holds_of,
    send,
    session_for,
    sets_of,
    settle,
    start,
    until_idle,
    verified,
)
from tests.merge_hold_helpers import LandingTree, Seen, SetSteps
from tests.proposal_fakes import Review, proposes
from tests.waiting import eventually

MUTATION = VerificationStep("Mutation", ("mutmut", "run"), "mutation")
STRESS = VerificationStep("Stress tests", ("stress",), "stress")
SOAK = CheckSet("soak", (VerificationStep("Soak", ("soak",), "soak"),), "holds_merges")
EVERY_10 = {"on": "commits", "every": 10, "checks": "mutation"}
LINT_STEP = {"name": "Lint", "command": ["ruff", "check"]}
MUTATION_STEP = {"name": "Mutation", "command": ["mutmut", "run"], "set": "mutation"}
STRESS_STEP = {"name": "Stress tests", "command": ["stress"], "set": "stress"}
HELD = "Merges held: stress failed on "
NO_REVIEW = {**TRIGGERS, "review": TRIGGERS["review"].model_copy(update={"enabled": False})}


def mutation_proposal(**trigger: object) -> dict[str, object]:
    """A proposal putting Mutation in the set mutation, which holds merges, run by the trigger
    mutation-every-10, with ``trigger``'s fields over its own."""
    return {
        "steps": [LINT_STEP, MUTATION_STEP],
        "sets": {"mutation": True},
        "triggers": {"mutation-every-10": {**EVERY_10, **trigger}},
    }


def proposing(proposal: dict[str, object]) -> Script:
    """A configurator that proposes ``proposal`` and completes."""
    script: Script = proposes(proposal, replies=[])
    return script


# Merged over those configured


def test_the_proposed_sets_come_first_then_each_configured_set_it_has_no_steps_in() -> None:
    proposal = ChecksProposal.model_validate(mutation_proposal())
    configured = (CheckSet("mutation", (STRESS,)), SOAK)
    assert merge_proposed_sets(configured, proposal) == (
        CheckSet("mutation", (MUTATION,), "holds_merges"),
        SOAK,
    )


def test_a_proposal_without_sets_keeps_every_configured_set() -> None:
    proposal = ChecksProposal.model_validate({"steps": [LINT_STEP]})
    assert merge_proposed_sets((SOAK,), proposal) == (SOAK,)


def test_a_kept_set_given_a_table_takes_the_effect_it_says() -> None:
    proposal = ChecksProposal.model_validate({"steps": [LINT_STEP], "sets": {"soak": False}})
    assert merge_proposed_sets((SOAK,), proposal) == (CheckSet("soak", SOAK.steps, "reports"),)


def test_the_window_says_whether_each_proposed_and_kept_set_holds_merges() -> None:
    proposal = ChecksProposal.model_validate(
        {"steps": [LINT_STEP, STRESS_STEP], "sets": {"soak": True}}
    )
    assert proposed("agent-1", "7", proposal).sets == {"stress": False, "soak": True}


def test_proposed_triggers_take_the_place_of_those_named_so_and_new_ones_come_last() -> None:
    configured = {
        "startup-check": Trigger(on="session_started", checks=TASK_CHECKS),
        "mutation-every-10": Trigger(on="commits", every=3, checks="mutation"),
        "soak": Trigger(on="commits", checks="soak"),
    }
    proposal = ChecksProposal.model_validate(
        {
            **mutation_proposal(),
            "triggers": {
                "mutation-every-10": EVERY_10,
                "nightly": {"on": "task_landed", "checks": "mutation"},
            },
        }
    )
    merged = merge_proposed_triggers(configured, {}, proposal)
    assert list(merged) == ["startup-check", "mutation-every-10", "soak", "nightly"]
    assert merged["mutation-every-10"] == Trigger.model_validate(EVERY_10)
    assert merged["soak"] == configured["soak"]


def test_a_proposed_trigger_is_merged_over_the_built_in_one_of_its_name() -> None:
    built_in = {"often": Trigger(on="commits", every=2, checks=TASK_CHECKS, enabled=False)}
    proposal = ChecksProposal.model_validate(
        {**mutation_proposal(), "triggers": {"often": {"on": "commits", "checks": "mutation"}}}
    )
    merged = merge_proposed_triggers(built_in, built_in, proposal)
    assert merged["often"] == Trigger(on="commits", every=2, checks="mutation", enabled=False)


# Used from the next task on


async def test_the_next_configurator_is_told_the_accepted_set_its_steps_effect_and_trigger() -> (
    None
):
    configurator = Configurator()
    session, _, _ = session_for(
        proposing(mutation_proposal()),
        configurator,
        verification=verified(Steps(*[True] * 4)),
        proposal_review=Review(ProposalDecision("accept")),
    )
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    await asyncio.wait_for(until_idle(session), 5)
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    await asyncio.wait_for(until_idle(session), 5)
    [prompt] = configurator.tasks
    assert "Task checks: every task, when orbit opens\n- Lint: ruff check" in prompt
    assert "Set mutation: every 10 commits, holds merges\n- Mutation: mutmut run" in prompt


def checkers(events: list[Event]) -> list[AgentStarted]:
    return [e for e in events if isinstance(e, AgentStarted) and e.agent_id == "set-mutation"]


def landed(tasks: int) -> Callable[[list[Event]], bool]:
    """What says ``tasks`` tasks completed and the set mutation started a run."""

    def done(events: list[Event]) -> bool:
        boards = [event.tasks for event in events if isinstance(event, TasksChanged)]
        finished = bool(boards) and [s.status for s in boards[-1]] == ["completed"] * tasks
        return finished and bool(checkers(events))

    return done


async def test_the_accepted_trigger_runs_its_set_at_its_commit_count() -> None:
    steps = Steps(*[True] * 8)
    session, _, _ = session_for(
        proposing(mutation_proposal(every=3)),
        claims_done,
        claims_done,
        verification=verified(steps),
        proposal_review=Review(ProposalDecision("accept")),
        tree=FakeTree(),
        review=True,
        triggers=NO_REVIEW,
    )
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    await asyncio.wait_for(until_idle(session), 5)
    await start(session, "One.", "Two.")
    events = await asyncio.wait_for(settle(session, until=landed(3)), 10)
    await session.close()
    assert len(checkers(events)) == 1
    assert steps.runs.count("Mutation") == 2  # Tried as proposed, then run by its trigger.


async def test_a_set_the_accepted_checks_make_hold_merges_holds_them_on_its_next_failure() -> None:
    steps = SetSteps(stress=[True, False])
    reporting = CheckSet("stress", (STRESS,))
    session, _, _ = session_for(
        proposing({"steps": [LINT_STEP, STRESS_STEP], "sets": {"stress": True}}),
        verification=Verification((LINT,), steps, sets=(reporting,)),
        proposal_review=Review(ProposalDecision("accept")),
        tree=LandingTree(),
        triggers=NO_REVIEW,
    )
    seen = Seen(session)
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    await asyncio.wait_for(until_idle(session), 5)
    sets, holds = sets_of(session), holds_of(session)
    assert sets is not None and holds is not None and not holds.held

    sets.run("stress", "nightly")
    await eventually(lambda: holds.held and any(n.startswith(HELD) for n in seen.notices()))
    seen.close()
    await session.close()
    assert steps.runs == ["stress", "stress"]
