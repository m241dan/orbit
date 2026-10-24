import pytest

from orbit.agent.domain.agent_task import (
    NO_CHECK_PROPOSALS,
    NO_STEPS,
    NO_SUGGESTIONS,
    AgentTask,
    Limits,
    invalid_proposal,
    unknown_step,
    visible_steps,
)
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import TaskSpec

STEP = VerificationStep(name="Unit tests", command=("pytest",))


def working(limits: Limits | None = None) -> AgentTask:
    agent_task = AgentTask(TaskSpec(prompt="Add a flag."), limits or Limits())
    agent_task.get_task()
    return agent_task


def verifying(limits: Limits | None = None) -> AgentTask:
    agent_task = working(limits)
    assert agent_task.claim_done("added it").verify
    return agent_task


def test_starts_assigned() -> None:
    assert AgentTask(TaskSpec(prompt="x"), Limits()).phase == "assigned"


def test_hands_out_its_prompt_and_starts_working() -> None:
    agent_task = AgentTask(TaskSpec(prompt="Add a flag."), Limits())
    brief = agent_task.get_task()
    assert "Add a flag." in brief and "complete_task" in brief and "give_up" in brief
    assert agent_task.phase == "working"


def test_without_steps_the_brief_names_no_checks() -> None:
    brief = AgentTask(TaskSpec(prompt="Add a flag."), Limits()).get_task()
    assert "checks" not in brief and "run_check" not in brief


def test_the_brief_names_the_checks_and_how_many_the_agent_may_run() -> None:
    agent_task = AgentTask(TaskSpec(prompt="Add a flag."), Limits(max_checks=7))
    brief = agent_task.get_task(("Ruff lint", "Unit tests, full coverage"), can_check=True)
    assert (
        "Orbit verifies your work with these checks, in order:"
        " `Ruff lint`, `Unit tests, full coverage`." in brief
    )
    assert "run each of them with run_check" in brief and "7 runs in all" in brief


def test_an_agent_without_run_check_is_told_the_checks_but_not_to_run_them() -> None:
    brief = AgentTask(TaskSpec(prompt="Add a flag."), Limits()).get_task(("Unit tests",))
    assert "Unit tests" in brief and "run_check" not in brief


def test_asking_again_hands_out_the_same_task_without_changing_phase() -> None:
    agent_task = verifying()
    assert "Add a flag." in agent_task.get_task()
    assert agent_task.phase == "verifying"


def test_claiming_done_starts_verification_and_keeps_the_summary() -> None:
    agent_task = working()
    claim = agent_task.claim_done("added the --fast flag")
    assert claim.verify and claim.reply is None
    assert agent_task.phase == "verifying"
    assert agent_task.summaries == ["added the --fast flag"]


def test_claiming_done_before_getting_the_task_is_refused() -> None:
    agent_task = AgentTask(TaskSpec(prompt="x"), Limits())
    claim = agent_task.claim_done("done")
    assert not claim.verify
    assert claim.reply is not None and "get_task" in claim.reply
    assert agent_task.phase == "assigned"


def test_a_second_claim_while_verifying_is_refused() -> None:
    agent_task = verifying()
    claim = agent_task.claim_done("done again")
    assert not claim.verify
    assert claim.reply is not None and "already" in claim.reply
    assert agent_task.summaries == ["added it"]


def test_claiming_done_after_finishing_is_refused() -> None:
    agent_task = verifying()
    agent_task.passed()
    claim = agent_task.claim_done("again")
    assert not claim.verify and claim.reply is not None and "finished" in claim.reply


def test_passing_finishes_and_tells_the_agent_to_stop() -> None:
    agent_task = verifying()
    assert "End your turn" in agent_task.passed()
    assert agent_task.phase == "done"


def test_failing_asks_for_a_fix_in_the_same_session() -> None:
    agent_task = verifying(Limits(max_attempts=5))
    reply = agent_task.failed(STEP, "1 failed")
    assert agent_task.phase == "working"
    assert agent_task.attempt == 1
    assert "Unit tests" in reply and "1 failed" in reply
    assert "Then call complete_task again." in reply and "1 of 5" in reply
    assert "run_check" not in reply


def test_an_agent_with_run_check_is_asked_to_run_the_failed_step_before_claiming() -> None:
    reply = verifying().failed(STEP, "1 failed", can_check=True)
    assert "Run that step with run_check once you've fixed it, then call complete_task" in reply


def test_failing_after_the_last_attempt_falls_back() -> None:
    agent_task = verifying(Limits(max_attempts=1))
    agent_task.failed(STEP, "1 failed")
    assert agent_task.claim_done("fixed").verify
    reply = agent_task.failed(STEP, "still failing")
    assert agent_task.phase == "fallback"
    assert agent_task.attempt == 1
    assert "End your turn" in reply


def test_exiting_while_working_counts_as_claiming_done() -> None:
    agent_task = working()
    assert agent_task.exited()
    assert agent_task.phase == "verifying"
    assert not agent_task.alive
    assert agent_task.summaries == []


def test_exiting_before_getting_the_task_needs_no_verification() -> None:
    agent_task = AgentTask(TaskSpec(prompt="x"), Limits())
    assert not agent_task.exited()
    assert agent_task.phase == "assigned"


@pytest.mark.parametrize("finish", ["passed", "verifying"])
def test_exiting_while_verifying_or_after_needs_no_new_verification(finish: str) -> None:
    agent_task = verifying()
    if finish == "passed":
        agent_task.passed()
    assert not agent_task.exited()


def test_a_failure_after_the_agent_exited_falls_back_even_with_attempts_left() -> None:
    agent_task = verifying(Limits(max_attempts=5))
    agent_task.exited()
    agent_task.failed(STEP, "1 failed")
    assert agent_task.phase == "fallback"
    assert agent_task.attempt == 0


def test_a_pass_after_the_agent_exited_still_finishes() -> None:
    agent_task = verifying()
    agent_task.exited()
    agent_task.passed()
    assert agent_task.phase == "done"


def test_giving_up_ends_the_task() -> None:
    agent_task = working()
    reply = agent_task.give_up("the spec contradicts itself")
    assert agent_task.phase == "gave_up"
    assert "End your turn" in reply


def test_giving_up_while_verifying_is_refused() -> None:
    agent_task = verifying()
    reply = agent_task.give_up("never mind")
    assert agent_task.phase == "verifying"
    assert "already" in reply


def test_checks_are_allowed_while_working_up_to_the_limit() -> None:
    agent_task = working(Limits(max_checks=2))
    assert agent_task.request_check() is None
    assert agent_task.request_check() is None
    refusal = agent_task.request_check()
    assert refusal is not None and "2" in refusal


def test_checks_are_refused_unless_working() -> None:
    assigned = AgentTask(TaskSpec(prompt="x"), Limits())
    refusal = assigned.request_check()
    assert refusal is not None and "get_task" in refusal
    busy = verifying().request_check()
    assert busy is not None and "already" in busy


def test_limits_default_to_five_attempts_and_twenty_checks() -> None:
    assert Limits() == Limits(max_attempts=5, max_checks=20)


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Limits(max_attempts=0)
    with pytest.raises(ValueError):
        Limits(max_checks=0)


def test_giving_up_or_checking_outside_work_is_refused_by_phase() -> None:
    assigned = AgentTask(TaskSpec(prompt="x"), Limits())
    assert "get_task" in assigned.give_up("no")
    finished = verifying()
    finished.passed()
    assert "finished" in finished.give_up("no")
    assert "finished" in (finished.request_check() or "")
    assert finished.phase == "done"


def test_every_accepted_claim_keeps_its_summary() -> None:
    agent_task = verifying()
    agent_task.failed(STEP, "1 failed")
    agent_task.claim_done("fixed the test")
    assert agent_task.summaries == ["added it", "fixed the test"]


def test_the_refusals_for_what_orbit_isnt_offering() -> None:
    assert NO_STEPS == "No verification steps are configured."
    assert NO_SUGGESTIONS == "Orbit isn't taking suggestions."
    assert NO_CHECK_PROPOSALS == "Orbit isn't taking check proposals."


def test_an_unknown_step_is_refused_with_the_steps_there_are() -> None:
    assert unknown_step(("Ruff lint", "Unit tests")) == (
        "No step by that name; the steps are: `Ruff lint`, `Unit tests`."
    )


def test_a_verified_agent_sees_every_step() -> None:
    lint = VerificationStep(name="Ruff lint", command=("ruff",))
    assert visible_steps([lint, STEP], verify=True) == (lint, STEP)


def test_an_unverified_agent_sees_no_steps() -> None:
    assert visible_steps([STEP], verify=False) == ()


def test_an_invalid_proposal_is_refused_with_why() -> None:
    assert invalid_proposal("no steps") == "Invalid proposal: no steps"
