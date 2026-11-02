"""Reports a configurator proposes beside its checks: a named set's run routed to a lens role,
read and refused as the config would read them, written as ``set_finished`` triggers that load,
shown in the proposal event, and used once accepted."""

import asyncio
import re
import tomllib
from collections.abc import Mapping, Sequence

import pytest

from orbit.agent.application.ports import ProposalDecision
from orbit.agent.application.proposals import proposed
from orbit.agent.domain.checks import ChecksProposal, merge_proposed_triggers, parse_proposal
from orbit.agent.domain.config import Package, load_layered
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.config_update import reference_with_checks, with_checks
from orbit.agent.domain.roles import Role, built_in_roles
from orbit.agent.domain.triggers import ReportTrigger, Trigger
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.agent.external.built_in_triggers import read_built_in_triggers
from orbit.agent.external.config_file import REFERENCE
from orbit.protocol import BUILT_IN_ROLE_NAMES, ProposedReport
from tests.agent_fakes import Steps, run, session_for, verified
from tests.proposal_fakes import CONFIGURE, Review, proposes

ROLES = built_in_roles(
    read_built_in_roles(), {name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES}
)
"""The built-in roles, with stand-in instructions."""

SHIPPED = read_built_in_triggers()
"""The shipped triggers."""

PROMPT = (
    "The {set} set {status} at {commit}.\n\n{output}\n\nReport each surviving mutant as a finding."
)
REPORT = {"set": "mutation", "role": "tests-reviewer", "prompt": PROMPT}
LINT = {"name": "Lint", "command": ["ruff", "check"]}
MUTANTS = {"name": "Mutants", "command": ["mutmut", "run"], "set": "mutation"}
EVERY_10 = {"on": "commits", "every": 10, "checks": "mutation"}
PROPOSAL: dict[str, object] = {
    "steps": [LINT, MUTANTS],
    "triggers": {"mutation-every-10": EVERY_10},
    "reports": {"mutation-report": REPORT},
}


def parse(
    data: dict[str, object],
    built_in: Mapping[str, Trigger] = SHIPPED,
    roles: Mapping[str, Role] = ROLES,
    configured: Sequence[VerificationStep] = (),
) -> ChecksProposal:
    """``data`` parsed as a proposal against the ``built_in`` triggers, the ``roles`` and the
    ``configured`` steps: the shipped triggers and the built-in roles, unless given."""
    return parse_proposal(data, built_in, configured, roles)


def refusal(
    data: dict[str, object],
    built_in: Mapping[str, Trigger] = SHIPPED,
    roles: Mapping[str, Role] = ROLES,
) -> str:
    """Why ``data`` is refused as a proposal (see :func:`parse`)."""
    with pytest.raises(ConfigError) as refused:
        parse(data, built_in, roles)
    return str(refused.value)


def with_report(**fields: object) -> dict[str, object]:
    """:data:`PROPOSAL` with ``fields`` over its report's own."""
    return PROPOSAL | {"reports": {"mutation-report": REPORT | fields}}


# Read and refused


def test_a_proposal_reads_its_reports_by_name_failing_by_default() -> None:
    assert parse(PROPOSAL).reports == {
        "mutation-report": ReportTrigger(set="mutation", role="tests-reviewer", prompt=PROMPT)
    }


def test_a_proposal_without_reports_has_none() -> None:
    assert parse({"steps": [LINT]}).reports == {}


def test_a_report_is_the_set_finished_trigger_queuing_its_role_when_the_set_fails() -> None:
    report = ReportTrigger(set="mutation", role="tests-reviewer", prompt=PROMPT)
    assert report.as_trigger() == Trigger(
        on="set_finished",
        role="tests-reviewer",
        prompt=PROMPT,
        when={"set": "mutation", "status": "failed"},
    )


def test_a_report_on_any_status_matches_every_run_of_its_set() -> None:
    report = ReportTrigger(set="mutation", role="tests-reviewer", prompt=PROMPT, status="any")
    assert report.as_trigger().when == {"set": "mutation"}


def test_a_report_to_a_role_not_granted_report_finding_is_refused() -> None:
    assert refusal(with_report(role="coder")).startswith(
        "reports.mutation-report.role: coder isn't an enabled role granted report_finding;"
        " those are "
    )


def test_a_report_to_a_disabled_finding_role_is_refused() -> None:
    disabled = ROLES["tests-reviewer"].model_copy(update={"enabled": False})
    roles = {**ROLES, "tests-reviewer": disabled}
    assert "tests-reviewer isn't an enabled role granted report_finding" in refusal(
        PROPOSAL, roles=roles
    )


def test_without_finding_roles_a_report_is_refused_saying_there_are_none() -> None:
    roles = {"coder": Role(instructions="Code.")}
    assert refusal(PROPOSAL, roles=roles) == (
        "reports.mutation-report.role: tests-reviewer isn't an enabled role granted"
        " report_finding; none is."
    )


def test_a_report_on_a_set_neither_proposed_nor_configured_is_refused() -> None:
    assert refusal(with_report(set="stress")) == (
        "reports.mutation-report.set: stress isn't a set in the proposal or the project; the"
        " sets are mutation."
    )


def test_a_report_on_a_set_with_no_sets_at_all_is_refused_saying_so() -> None:
    data: dict[str, object] = {"steps": [LINT], "reports": {"mutation-report": REPORT}}
    assert refusal(data) == (
        "reports.mutation-report.set: mutation isn't a set in the proposal or the project; no"
        " step is in a set."
    )


def test_a_report_may_name_a_set_only_the_project_configures() -> None:
    configured = (VerificationStep("Mutants", ("mutmut", "run"), "mutation"),)
    data: dict[str, object] = {"steps": [LINT], "reports": {"mutation-report": REPORT}}
    assert parse(data, configured=configured).reports["mutation-report"].set == "mutation"


def test_a_report_whose_prompt_uses_another_variable_is_refused() -> None:
    assert refusal(with_report(prompt="Review {diff}.")) == (
        "reports.mutation-report.prompt: {diff} isn't a variable of set_finished; its variables"
        " are {trigger}, {set}, {status}, {commit}, {output}."
    )


def test_a_report_named_like_a_built_in_trigger_is_refused() -> None:
    built_in = {"review": Trigger(on="task_landed", role="reviewer", prompt="Review {commit}.")}
    data = PROPOSAL | {"reports": {"review": REPORT}}
    assert refusal(data, built_in=built_in) == (
        "reports.review: review is taken by a built-in trigger; give this report another name."
    )


def test_a_report_named_like_a_shipped_trigger_is_refused() -> None:
    data = PROPOSAL | {"reports": {"tests-review": REPORT}}
    assert refusal(data).startswith("reports.tests-review: tests-review is taken by a built-in")


def test_a_report_named_like_a_proposed_trigger_is_refused() -> None:
    data = PROPOSAL | {"reports": {"mutation-every-10": REPORT}}
    assert refusal(data) == (
        "reports.mutation-every-10: mutation-every-10 is taken by a proposed trigger; give this"
        " report another name."
    )


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"status": "passed"}, "reports.mutation-report.status"),
        ({"prompt": ""}, "reports.mutation-report.prompt"),
        ({"every": 3}, "reports.mutation-report.every"),
    ],
)
def test_a_malformed_report_is_refused_naming_its_field(
    fields: dict[str, object], message: str
) -> None:
    assert message in refusal(with_report(**fields))


def test_a_report_needs_a_plain_name() -> None:
    assert "a trigger's name" in refusal(PROPOSAL | {"reports": {"a b": REPORT}})


def test_each_bad_report_is_refused_on_a_line_of_its_own() -> None:
    reports = {"one": REPORT | {"role": "coder"}, "two": REPORT | {"set": "stress"}}
    lines = refusal(PROPOSAL | {"reports": reports}).split("\n")
    assert [line.split(":")[0] for line in lines] == ["reports.one.role", "reports.two.set"]


# Written and loaded


def loaded_triggers(text: str) -> dict[str, Trigger]:
    """The triggers of the configuration a project file holding ``text`` makes."""
    package = Package(ROLES, SHIPPED)
    return load_layered(tomllib.loads(text), package).config.triggers


MUTATION_REPORT = ReportTrigger(set="mutation", role="tests-reviewer", prompt=PROMPT)
CHECKS = ChecksProposal(
    steps=(
        VerificationStep("Lint", ("ruff", "check")),
        VerificationStep("Mutants", ("mutmut", "run"), "mutation"),
    ),
    reports={"mutation-report": MUTATION_REPORT},
)


def test_an_accepted_report_is_written_as_a_set_finished_trigger_and_loads() -> None:
    written = with_checks("", CHECKS)
    assert tomllib.loads(written)["triggers"] == {
        "mutation-report": {
            "on": "set_finished",
            "role": "tests-reviewer",
            "when": {"set": "mutation", "status": "failed"},
            "prompt": PROMPT,
        }
    }
    assert '[triggers.mutation-report]\non = "set_finished"\nrole = "tests-reviewer"\n' in written
    assert 'when = {set = "mutation", status = "failed"}' in written
    assert loaded_triggers(written)["mutation-report"] == MUTATION_REPORT.as_trigger()


def test_a_report_on_any_status_writes_no_status() -> None:
    report = MUTATION_REPORT.model_copy(update={"status": "any"})
    written = with_checks("", CHECKS.model_copy(update={"reports": {"mutation-report": report}}))
    assert tomllib.loads(written)["triggers"]["mutation-report"]["when"] == {"set": "mutation"}
    assert loaded_triggers(written)["mutation-report"].when == {"set": "mutation"}


def test_a_report_is_written_beside_the_proposed_triggers() -> None:
    every_10 = parse(PROPOSAL).triggers["mutation-every-10"]
    checks = CHECKS.model_copy(update={"triggers": {"mutation-every-10": every_10}})
    written = with_checks("", checks)
    assert list(tomllib.loads(written)["triggers"]) == ["mutation-every-10", "mutation-report"]
    assert loaded_triggers(written)["mutation-every-10"] == every_10.as_trigger()


def test_a_one_line_prompt_is_written_as_a_plain_string() -> None:
    report = MUTATION_REPORT.model_copy(update={"prompt": "Report {set}."})
    written = with_checks("", CHECKS.model_copy(update={"reports": {"mutation-report": report}}))
    assert written.endswith('prompt = "Report {set}."\n')


def test_a_report_rewritten_into_a_file_setting_triggers_inline_keeps_the_others() -> None:
    text = '[triggers]\nnightly = { on = "manual", label = "N", checks = "mutation" }\n'
    written = with_checks(text, CHECKS)
    triggers = tomllib.loads(written)["triggers"]
    assert list(triggers) == ["nightly", "mutation-report"]
    assert triggers["mutation-report"]["when"] == {"set": "mutation", "status": "failed"}


def test_a_report_is_appended_to_the_reference_config_and_loads() -> None:
    written = reference_with_checks(REFERENCE.read_text(encoding="utf-8"), CHECKS)
    assert loaded_triggers(written)["mutation-report"] == MUTATION_REPORT.as_trigger()


def test_writing_the_same_report_again_changes_nothing() -> None:
    once = with_checks("", CHECKS)
    assert with_checks(once, CHECKS) == once


def test_an_accepted_report_is_used_as_its_trigger_from_then_on() -> None:
    configured = {"soak": Trigger(on="commits", checks="soak")}
    merged = merge_proposed_triggers(configured, SHIPPED, CHECKS)
    assert list(merged) == ["soak", "mutation-report"]
    assert merged["mutation-report"] == MUTATION_REPORT.as_trigger()


# Shown and proposed through a session


def test_the_proposal_event_carries_each_report_by_name() -> None:
    event = proposed("agent-1", "7", parse(PROPOSAL))
    assert event.reports == {
        "mutation-report": ProposedReport(set="mutation", role="tests-reviewer", prompt=PROMPT)
    }


def test_a_proposal_event_without_reports_has_none() -> None:
    assert proposed("agent-1", "7", parse({"steps": [LINT]})).reports == {}


async def test_a_session_refuses_a_report_to_a_role_that_cant_report_findings() -> None:
    replies: list[str] = []
    review = Review(ProposalDecision("accept"))
    session, _, _ = session_for(
        proposes(with_report(role="coder"), PROPOSAL, replies=replies),
        verification=verified(Steps(True, True)),
        proposal_review=review,
    )
    await asyncio.wait_for(run(session, CONFIGURE), 5)
    assert re.search("reports.mutation-report.role: coder isn't an enabled role", replies[0])
    [(_, accepted)] = review.proposals
    assert accepted.reports == {"mutation-report": MUTATION_REPORT}
