"""What a configurator is told is configured now: the task checks and setup as a list, or, with
steps in named sets, the task checks, then each set with its steps, when its triggers run it and
whether it holds merges; and when a trigger fires, in words."""

import pytest

from orbit.agent.domain.checks import Setup, configured_checks, describe_checks
from orbit.agent.domain.machine import ADVICE, Bytes, Machine
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


CONFIGURED = "Configured now (build on these):\n\nSteps:\n- Lint: ruff check"


@pytest.mark.parametrize(
    ("steps", "machine", "checks"),
    [
        (
            (LINT,),
            Machine(cpus=4, memory=Bytes(8 * 2**30)),
            f"{CONFIGURED}\n\nThis machine: 4 CPUs, 8.0 GiB of memory. {ADVICE}",
        ),
        ((LINT,), Machine(cpus=2), f"{CONFIGURED}\n\nThis machine: 2 CPUs. {ADVICE}"),
        ((LINT,), Machine(), CONFIGURED),
        (
            (),
            Machine(cpus=1),
            f"Nothing is configured yet.\n\nThis machine: 1 CPU. {ADVICE}",
        ),
        ((), Machine(), "Nothing is configured yet."),
    ],
)
def test_the_machine_line_follows_a_blank_line_when_anything_of_the_machine_is_known(
    steps: tuple[VerificationStep, ...], machine: Machine, checks: str
) -> None:
    assert configured_checks(steps, None, (), {}, machine) == checks


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
        ({"on": "reviews_finished"}, "when a review's lenses have finished"),
        ({"on": "set_finished"}, "when a set finishes"),
        ({"on": "manual", "manual": True, "label": "Run"}, "by hand"),
        ({"on": "task_landed", "manual": True, "label": "Run"}, "when a task lands or by hand"),
        ({"on": "commits", "every": 5, "enabled": False}, "every 5 commits (off)"),
    ],
)
def test_when_a_trigger_fires_is_said_in_words(fields: dict[str, object], when: str) -> None:
    assert describe_firing(Trigger.model_validate({"checks": "slow", **fields})) == when


def report_trigger(name: str = "slow", **when: str) -> Trigger:
    """A ``set_finished`` trigger queuing a tests-reviewer task when set ``name`` finishes, with
    ``when`` fields over that."""
    return Trigger(
        on="set_finished", role="tests-reviewer", prompt="Look", when={"set": name, **when}
    )


def test_a_set_failure_report_is_named_under_its_set_after_its_steps() -> None:
    triggers = {
        "mutation": checks_trigger("slow"),
        "mutation-report": report_trigger(status="failed"),
    }
    assert configured_checks((LINT,), None, (SLOW,), triggers) == (
        "Configured now (build on these):\n\n"
        "Task checks: every task\n- Lint: ruff check\n\n"
        "Set slow: every 10 commits, holds merges\n- mutation: mutmut run\n"
        "Report mutation-report: slow → tests-reviewer when it fails"
    )


@pytest.mark.parametrize(
    ("when", "phrase"),
    [
        ({}, "when it finishes"),
        ({"status": "passed"}, "when it passes"),
        ({"status": ("passed", "failed")}, "when it finishes"),
    ],
)
def test_a_reports_status_is_said_in_words(when: dict[str, object], phrase: str) -> None:
    trigger = Trigger.model_validate(
        {"on": "set_finished", "role": "tests-reviewer", "when": {"set": "slow", **when}}
    )
    described = describe_checks((), None, (SLOW,), {"report": trigger})
    assert described.endswith(f"\nReport report: slow → tests-reviewer {phrase}")


def test_a_report_on_several_sets_is_named_under_each_with_its_roles() -> None:
    trigger = Trigger(
        on="set_finished",
        role=("tests-reviewer", "scaling-reviewer"),
        when={"set": ("slow", "perf")},
        enabled=False,
    )
    described = describe_checks((), None, (SLOW, PERF), {"both": trigger})
    assert (
        "- mutation: mutmut run\nReport both: slow → tests-reviewer, scaling-reviewer" in described
    )
    assert described.endswith(
        "- bench: pytest benchmarks\n"
        "Report both: perf → tests-reviewer, scaling-reviewer when it finishes (off)"
    )


def test_reports_on_other_sets_other_events_or_without_a_role_are_not_listed() -> None:
    triggers = {
        "elsewhere": report_trigger("perf"),
        "landed": Trigger(on="task_landed", role="tests-reviewer", prompt="Look"),
        "rerun": Trigger(on="set_finished", checks="perf", when={"set": "slow"}),
    }
    described = describe_checks((LINT,), None, (SLOW,), triggers)
    assert "Report" not in described
