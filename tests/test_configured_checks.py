"""What a configurator is told is configured now: the task checks and setup as a list, or, with
steps in named sets, the task checks, then each set with its steps, when its triggers run it and
whether it holds merges; and when a trigger fires, in words."""

import pytest

from orbit.agent.domain.checks import Setup, configured_checks, describe_checks
from orbit.agent.domain.triggers import Trigger, describe_firing
from orbit.agent.domain.verification import CheckSet, VerificationStep
from orbit.protocol import TASK_CHECKS

LINT = VerificationStep("Lint", ("ruff", "check"))
TESTS = VerificationStep("Tests", ("pytest",))
MUTATION = VerificationStep("mutation", ("mutmut", "run"), "slow")
BENCH = VerificationStep("bench", ("pytest", "benchmarks"), "perf")
SETUP = Setup(command=("uv", "sync"), watch=("uv.lock",))
SLOW = CheckSet("slow", (MUTATION,), "holds_merges")
PERF = CheckSet("perf", (BENCH,))


def checks_trigger(checks: str, **fields: object) -> Trigger:
    """A trigger running ``checks`` every 10 commits, with ``fields`` over that."""
    return Trigger.model_validate({"on": "commits", "every": 10, "checks": checks, **fields})


def test_with_task_checks_only_the_text_is_todays() -> None:
    assert configured_checks((LINT, TESTS), SETUP) == (
        "Configured now (build on these):\n\n"
        "Steps:\n- Lint: ruff check\n- Tests: pytest\n\n"
        "Setup: uv sync (again when uv.lock changes)"
    )


def test_with_nothing_configured_it_says_so() -> None:
    assert configured_checks((), None, (), {}) == "Nothing is configured yet."


def test_with_steps_in_sets_each_set_is_named_with_its_steps_triggers_and_holds() -> None:
    triggers = {
        "startup-check": Trigger(on="session_started", checks=TASK_CHECKS),
        "mutation": checks_trigger("slow"),
        "bench": Trigger(on="manual", checks="perf", manual=True, label="Bench"),
        "look": Trigger(on="task_landed", role="reviewer", prompt="Look"),
    }
    assert configured_checks((LINT, TESTS), SETUP, (SLOW, PERF), triggers) == (
        "Configured now (build on these):\n\n"
        "Task checks: every task, when orbit opens\n- Lint: ruff check\n- Tests: pytest\n\n"
        "Set slow: every 10 commits, holds merges\n- mutation: mutmut run\n\n"
        "Set perf: by hand, reports\n- bench: pytest benchmarks\n\n"
        "Setup: uv sync (again when uv.lock changes)"
    )


def test_a_set_no_trigger_runs_says_so_and_no_task_checks_say_none() -> None:
    assert describe_checks((), None, (PERF,), {}) == (
        "Task checks: every task\nnone\n\nSet perf: no trigger, reports\n- bench: pytest benchmarks"
    )


def test_each_trigger_running_a_set_is_named_in_its_order() -> None:
    triggers = {"often": checks_trigger("slow", every=1), "rarely": checks_trigger("slow")}
    described = describe_checks((LINT,), None, (SLOW,), triggers)
    assert "Set slow: every commit, every 10 commits, holds merges" in described


@pytest.mark.parametrize(
    ("fields", "when"),
    [
        ({"on": "commits"}, "every commit"),
        ({"on": "commits", "every": 3}, "every 3 commits"),
        ({"on": "session_started"}, "when orbit opens"),
        ({"on": "task_landed"}, "when a task lands"),
        ({"on": "task_finished"}, "when a task finishes"),
        ({"on": "startup_check_failed"}, "when the startup check fails"),
        ({"on": "manual", "manual": True, "label": "Run"}, "by hand"),
        ({"on": "task_landed", "manual": True, "label": "Run"}, "when a task lands or by hand"),
        ({"on": "commits", "every": 5, "enabled": False}, "every 5 commits (off)"),
    ],
)
def test_when_a_trigger_fires_is_said_in_words(fields: dict[str, object], when: str) -> None:
    assert describe_firing(Trigger.model_validate({"checks": "slow", **fields})) == when
