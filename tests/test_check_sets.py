"""Check sets in the configuration: a step's set, the task checks every task runs, the sets'
tables, the triggers that run them, and what's refused or noted at load."""

from typing import Any

import pytest
from pydantic import TypeAdapter

from orbit.agent.domain.checks import VerificationSet
from orbit.agent.domain.config import OrbitConfig, Package, load_layered
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.roles import built_in_roles
from orbit.agent.domain.triggers import Trigger, verification_state
from orbit.agent.domain.verification import (
    CheckSet,
    VerificationStep,
    set_failed_away_note,
    set_failed_note,
    trim_output,
)
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.agent.external.built_in_triggers import read_built_in_triggers
from orbit.protocol import BUILT_IN_ROLE_NAMES, TASK_CHECKS, StepState
from orbit.tui.view.panels import VerificationSteps
from tests.view_fakes import StepsView

INSTRUCTIONS = {name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES}
ROLES = built_in_roles(read_built_in_roles(), INSTRUCTIONS)
SHIPPED = read_built_in_triggers()
A = {"name": "A", "command": ["a"]}
B = {"name": "B", "command": ["b"]}
C = {"name": "C", "command": ["c"], "set": "slow"}
SLOW_CHECK = {"on": "commits", "checks": "slow"}
MANUAL_CHECK = {"on": "manual", "checks": "task-checks", "manual": True, "label": "Check"}


def loaded(data: dict[str, Any]) -> OrbitConfig:
    """The configuration a project file holding ``data`` makes over the package's roles and
    triggers."""
    return load_layered(data, Package(ROLES, SHIPPED)).config


def with_steps(*steps: dict[str, Any], **data: Any) -> OrbitConfig:
    """The configuration of a project file with ``steps`` and ``data``, ``slow`` run every
    landing unless ``data`` sets the triggers."""
    return loaded({"verification": {"steps": list(steps)}, "triggers": {"slow": SLOW_CHECK}} | data)


def refusal(data: dict[str, Any]) -> str:
    """Why a project file holding ``data`` is refused."""
    with pytest.raises(ConfigError) as raised:
        loaded(data)
    return str(raised.value)


# A step's set and the task checks


def test_a_step_is_in_no_set_unless_it_names_one() -> None:
    assert VerificationStep("A", ("a",)).set is None
    assert with_steps(A, C).verification.steps[1] == VerificationStep("C", ("c",), set="slow")


def test_the_task_checks_are_the_steps_in_no_set_in_order() -> None:
    verification = with_steps(A, C, B).verification
    assert verification.task_checks == (
        VerificationStep("A", ("a",)),
        VerificationStep("B", ("b",)),
    )


def test_without_a_set_anywhere_the_task_checks_are_every_step() -> None:
    verification = loaded({"verification": {"steps": [A, B]}}).verification
    assert verification.task_checks == verification.steps
    assert verification.set_names == ()


def test_the_set_names_are_those_steps_are_in_each_once_in_order() -> None:
    fast = {"name": "D", "command": ["d"], "set": "fast"}
    slower = C | {"name": "E"}
    triggers = {"slow": SLOW_CHECK, "fast": {"on": "commits", "checks": "fast"}}
    config = with_steps(C, A, fast, slower, triggers=triggers)
    assert config.verification.set_names == ("slow", "fast")


def test_with_only_steps_in_sets_verification_is_missing() -> None:
    assert verification_state(with_steps(C).verification.task_checks) == "missing"


def test_a_step_has_a_description_of_its_set() -> None:
    properties = TypeAdapter(VerificationStep).json_schema()["properties"]
    assert properties["set"]["description"] == (
        "The named set it's in; left out, it's one of the task checks every task runs."
    )


# The sets' tables


def test_a_sets_table_says_whether_it_holds_merges() -> None:
    config = with_steps(verification={"steps": [A, C], "sets": {"slow": {}}})
    assert config.verification.sets == {"slow": VerificationSet()}
    assert not config.verification.sets["slow"].holds_merges
    held = {"steps": [A, C], "sets": {"slow": {"holds_merges": True}}}
    assert with_steps(verification=held).verification.sets == {
        "slow": VerificationSet(holds_merges=True)
    }


def test_without_sets_tables_there_are_none() -> None:
    assert with_steps(A, C).verification.sets == {}


# The sets as triggers run them


def test_each_set_has_its_steps_in_order_and_reports_without_a_table() -> None:
    d = {"name": "D", "command": ["d"], "set": "nightly"}
    e = {"name": "E", "command": ["e"], "set": "slow"}
    triggers = {"slow": SLOW_CHECK, "nightly": {"on": "commits", "checks": "nightly"}}
    sets = with_steps(A, C, d, e, triggers=triggers).verification.check_sets
    assert sets == (
        CheckSet(
            "slow", (VerificationStep("C", ("c",), "slow"), VerificationStep("E", ("e",), "slow"))
        ),
        CheckSet("nightly", (VerificationStep("D", ("d",), "nightly"),), "reports"),
    )


def test_a_set_whose_table_holds_merges_holds_them() -> None:
    held = {"steps": [A, C], "sets": {"slow": {"holds_merges": True}}}
    [slow] = with_steps(verification=held).verification.check_sets
    assert slow.effect == "holds_merges"


def test_without_a_set_anywhere_there_are_no_check_sets() -> None:
    assert with_steps(A, B, triggers={}).verification.check_sets == ()


def test_a_reporting_sets_failure_points_at_the_verification_panel() -> None:
    assert set_failed_note("slow", "Mutation tests") == (
        "slow reported: Mutation tests failed (see the verification panel)"
    )


def test_a_reporting_sets_failure_away_from_the_panel_quotes_its_outputs_last_lines() -> None:
    output = "\n".join(f"line {number}" for number in range(1, 13)) + "\n\n"
    tail = "\n".join(f"line {number}" for number in range(3, 13))
    assert set_failed_away_note("slow", "Mutation tests", output) == (
        f"slow reported: Mutation tests failed. Its output ended:\n\n{tail}"
    )


def test_a_reporting_sets_failure_away_from_the_panel_quotes_its_output_trimmed() -> None:
    assert set_failed_away_note("slow", "Soak", "  \n 3 mutants survived \n") == (
        "slow reported: Soak failed. Its output ended:\n\n3 mutants survived"
    )


def test_a_reporting_sets_failure_away_from_the_panel_without_output_says_so() -> None:
    assert (
        set_failed_away_note("slow", "Soak", " \n ") == "slow reported: Soak failed, with no output"
    )


PROGRESS = "run\r10%\r20%\r30%\r40%\r50%\r60%\r70%\r80%\r90%\r100%\r\nform\x0cfeed\nFAILED x\n"


def test_a_reporting_sets_failure_away_from_the_panel_splits_its_output_only_at_newlines() -> None:
    assert set_failed_away_note("slow", "Mutation tests", PROGRESS) == (
        "slow reported: Mutation tests failed. Its output ended:\n\n"
        "run\r10%\r20%\r30%\r40%\r50%\r60%\r70%\r80%\r90%\r100%\nform\x0cfeed\nFAILED x"
    )


@pytest.mark.parametrize(
    "output",
    [PROGRESS, "\n".join(f"line {number}" for number in range(1, 30)), "a\x0bb\x1cc\u2028d\n"],
)
def test_a_reporting_sets_away_notice_quotes_the_lines_the_panel_shows(output: str) -> None:
    view = StepsView()
    steps = VerificationSteps(view, lambda: 0.0)
    steps.show([StepState(name="Mutation tests", status="failed")], "slow", trim_output(output))
    note = set_failed_away_note("slow", "Mutation tests", output)
    panel_tail = view.texts[-1].split("\n", 2)[2]
    assert note.split("\n\n", 1)[1] == panel_tail


def test_holds_merges_has_its_description() -> None:
    assert VerificationSet.model_fields["holds_merges"].description == (
        "Whether its failure holds merges until a fix lands; otherwise it only reports."
    )


@pytest.mark.parametrize(
    ("verification", "problem"),
    [
        (
            {"steps": [A, C], "sets": {"none": {}}},
            'verification.sets.none: no step is in it; a step joins it with set = "none".',
        ),
        (
            {"steps": [A, C | {"set": "task-checks"}]},
            "verification.steps.2.set: task-checks names the steps in no set; a set takes"
            " another name.",
        ),
        (
            {"steps": [A], "sets": {"task-checks": {}}},
            "verification.sets.task-checks: task-checks names the steps in no set; a set takes"
            " another name.",
        ),
        (
            {"steps": [C | {"set": "slow.er"}]},
            "verification.steps.1.set: a set's name is a plain name: letters, digits, - and _",
        ),
        (
            {"steps": [A, C], "sets": {"slow": {"holds_merges": "often"}}},
            "verification.sets.slow.holds_merges: Input should be a valid boolean, unable to"
            " interpret input",
        ),
    ],
)
def test_a_bad_set_is_refused_naming_its_setting(
    verification: dict[str, Any], problem: str
) -> None:
    assert refusal({"verification": verification}) == problem


def test_each_set_no_step_is_in_is_refused_on_a_line_of_its_own() -> None:
    sets: dict[str, Any] = {"none": {}, "slow": {}, "other": {}}
    assert refusal({"verification": {"steps": [A, C], "sets": sets}}) == (
        'verification.sets.none: no step is in it; a step joins it with set = "none".\n'
        'verification.sets.other: no step is in it; a step joins it with set = "other".'
    )


# The triggers that run them


def test_a_trigger_runs_the_task_checks_or_a_set() -> None:
    config = with_steps(A, C, triggers={"slow": SLOW_CHECK, "check": MANUAL_CHECK})
    assert config.triggers["slow"].checks == "slow"
    assert config.triggers["check"].checks == TASK_CHECKS


def test_gate_is_read_as_the_task_checks() -> None:
    assert Trigger(on="session_started", checks="gate").checks == TASK_CHECKS
    config = loaded({"triggers": {"startup-check": {"checks": "gate"}}})
    assert config.triggers["startup-check"] == SHIPPED["startup-check"]


def test_the_shipped_startup_check_runs_the_task_checks() -> None:
    assert SHIPPED["startup-check"].checks == "task-checks"


@pytest.mark.parametrize(
    ("steps", "problem"),
    [
        (
            [A, C],
            'triggers.nope.checks: nope isn\'t "task-checks" or a set a step is in; the sets are'
            " slow.",
        ),
        (
            [A],
            'triggers.nope.checks: nope isn\'t "task-checks" or a set a step is in; no step is in'
            " a set.",
        ),
    ],
)
def test_a_trigger_naming_a_set_no_step_is_in_is_refused(
    steps: list[dict[str, Any]], problem: str
) -> None:
    triggers = {"slow": SLOW_CHECK, "nope": {"on": "commits", "checks": "nope"}}
    data = {"verification": {"steps": steps}, "triggers": triggers}
    assert problem in refusal(data).splitlines()


# The notice of a set no trigger runs


def test_a_set_no_trigger_runs_is_noted() -> None:
    config = with_steps(A, C, triggers={})
    assert config.notices == ("The set slow has no trigger, so it never runs.",)


def test_a_set_only_a_disabled_trigger_runs_is_noted() -> None:
    config = with_steps(A, C, triggers={"slow": SLOW_CHECK | {"enabled": False}})
    assert config.notices == ("The set slow has no trigger, so it never runs.",)


def test_a_set_a_trigger_runs_isnt_noted() -> None:
    assert with_steps(A, C).notices == ()


def test_the_task_checks_need_no_trigger_of_their_own() -> None:
    triggers = {"startup-check": {"enabled": False}}
    assert loaded({"verification": {"steps": [A]}, "triggers": triggers}).notices == ()
