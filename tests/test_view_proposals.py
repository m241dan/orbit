"""How the TUI words proposed checks and the user's decisions on them."""

import pytest

from orbit.protocol import (
    ChecksProposed,
    DecideProposal,
    ProposalDecided,
    ProposedReport,
    ProposedStep,
    ProposedTrigger,
)
from orbit.tui.view.proposals import (
    FEEDBACK_HINT,
    FEEDBACK_PLACEHOLDER,
    PROPOSAL_KEYS_HINT,
    decision_entry,
    proposal_text,
    proposal_title,
    revision,
)

PROPOSAL = ChecksProposed(
    agent_id="agent-3", task_id="7", steps=(ProposedStep(name="Lint", command=("ruff",)),)
)


def test_proposal_title_names_the_proposing_agent() -> None:
    assert proposal_title("agent-3") == "Proposed checks · agent-3"


def test_proposal_hints_name_the_keys() -> None:
    assert PROPOSAL_KEYS_HINT == "a accept · r revise · x end"
    assert FEEDBACK_HINT == "enter send · esc back"


def test_feedback_box_asks_what_should_change() -> None:
    assert FEEDBACK_PLACEHOLDER == "What should change?"


def test_feedback_asks_for_its_changes_trimmed() -> None:
    assert revision(PROPOSAL, "  add mypy ") == DecideProposal(
        task_id="7", action="revise", feedback="add mypy"
    )


def test_blank_feedback_asks_for_nothing() -> None:
    assert revision(PROPOSAL, " ") is None
    assert revision(PROPOSAL, "") is None


def test_a_decision_is_noted_as_orbit_telling_the_agent() -> None:
    accept = DecideProposal(task_id="7", action="accept")
    end = DecideProposal(task_id="7", action="end")
    revise = DecideProposal(task_id="7", action="revise", feedback="add mypy")
    assert decision_entry("agent-3", accept) == ("orbit → agent-3: proposal accepted", "agent")
    assert decision_entry("agent-3", end) == ("orbit → agent-3: task ended", "agent")
    assert decision_entry("agent-3", revise) == (
        "orbit → agent-3: changes asked for: add mypy",
        "agent",
    )


def test_another_frontends_decision_is_noted_the_same_way() -> None:
    decided = ProposalDecided(
        command_id="c", agent_id="agent-3", task_id="7", action="revise", feedback="More."
    )
    assert decision_entry("agent-3", decided) == (
        "orbit → agent-3: changes asked for: More.",
        "agent",
    )


WITH_SET = ChecksProposed(
    agent_id="agent-3",
    task_id="7",
    steps=(
        ProposedStep(name="A", command=("a",)),
        ProposedStep(name="B", command=("b",)),
        ProposedStep(name="C", command=("c",), set="mutation"),
    ),
    sets={"mutation": False},
    triggers={"mutation-every-10": ProposedTrigger(on="commits", every=10, checks="mutation")},
)


def test_a_proposal_with_a_set_shows_the_task_checks_then_each_set_and_when_it_runs() -> None:
    assert proposal_text(WITH_SET) == (
        "Setup: none\n"
        "\n"
        "Task checks: every task\n"
        "  1. A: a\n"
        "  2. B: b\n"
        "\n"
        "Set mutation: every 10 commits, reports\n"
        "  3. C: c\n"
        "\n"
        "✅ All passed on the current code."
    )


def test_steps_keep_their_numbers_under_their_sets() -> None:
    steps = (
        ProposedStep(name="Slow", command=("s",), set="stress"),
        ProposedStep(name="Lint", command=("l",)),
    )
    proposal = ChecksProposed(agent_id="a", task_id="7", steps=steps, sets={"stress": True})
    assert proposal_text(proposal) == (
        "Setup: none\n"
        "\n"
        "Task checks: every task\n"
        "  2. Lint: l\n"
        "\n"
        "Set stress: no trigger, holds merges\n"
        "  1. Slow: s\n"
        "\n"
        "✅ All passed on the current code."
    )


@pytest.mark.parametrize(
    ("trigger", "shown"),
    [
        (ProposedTrigger(on="commits", checks="stress"), "every commit"),
        (ProposedTrigger(on="commits", every=1, checks="stress"), "every commit"),
        (ProposedTrigger(on="manual", manual=True, label="S", checks="stress"), "by hand"),
        (ProposedTrigger(on="task_landed", checks="stress"), "when a task lands"),
        (ProposedTrigger(on="task_finished", checks="stress"), "when a task finishes"),
        (ProposedTrigger(on="session_started", checks="stress"), "when orbit opens"),
        (
            ProposedTrigger(on="startup_check_failed", checks="stress"),
            "when the startup check fails",
        ),
        (ProposedTrigger(on="later", checks="stress"), "on later"),
        (
            ProposedTrigger(on="commits", every=5, manual=True, label="S", checks="stress"),
            "every 5 commits or by hand",
        ),
        (ProposedTrigger(on="commits", checks="stress", enabled=False), "every commit (off)"),
    ],
)
def test_a_sets_triggers_say_when_it_runs(trigger: ProposedTrigger, shown: str) -> None:
    proposal = ChecksProposed(
        agent_id="a",
        task_id="7",
        steps=(ProposedStep(name="Slow", command=("s",), set="stress"),),
        sets={"stress": False},
        triggers={"t": trigger},
    )
    assert f"Set stress: {shown}, reports\n  1. Slow: s\n" in proposal_text(proposal)


def test_the_task_checks_name_the_triggers_that_run_them_and_may_be_none() -> None:
    by_hand = ProposedTrigger(on="manual", manual=True, label="Check", checks="task-checks")
    proposal = ChecksProposed(
        agent_id="a",
        task_id="7",
        steps=(ProposedStep(name="Slow", command=("s",), set="stress"),),
        sets={"stress": False},
        triggers={"check": by_hand},
    )
    assert "Task checks: every task, by hand\n  none\n\nSet stress: no trigger, reports\n" in (
        proposal_text(proposal)
    )


def test_task_checks_with_a_trigger_show_when_they_run() -> None:
    every_five = ProposedTrigger(on="commits", every=5, checks="task-checks")
    proposal = ChecksProposed(
        agent_id="a",
        task_id="7",
        steps=(ProposedStep(name="Lint", command=("ruff",)),),
        triggers={"often": every_five},
    )
    assert proposal_text(proposal) == (
        "Setup: none\n"
        "\n"
        "Task checks: every task, every 5 commits\n"
        "  1. Lint: ruff\n"
        "\n"
        "✅ All passed on the current code."
    )


def test_a_proposal_with_no_sets_or_triggers_lists_its_steps() -> None:
    assert proposal_text(PROPOSAL) == (
        "Setup: none\n\nSteps:\n  1. Lint: ruff\n\n✅ All passed on the current code."
    )


MANUAL_MUTATION = ProposedTrigger(on="manual", label="Mutation", checks="mutation")


def test_a_kept_set_given_a_table_shows_whether_it_holds_merges_and_keeps_its_steps() -> None:
    proposal = ChecksProposed(
        agent_id="a",
        task_id="7",
        steps=(ProposedStep(name="Lint", command=("ruff",)),),
        sets={"mutation": True},
    )
    assert proposal_text(proposal) == (
        "Setup: none\n"
        "\n"
        "Task checks: every task\n"
        "  1. Lint: ruff\n"
        "\n"
        "Set mutation: no trigger, holds merges\n"
        "  steps kept as configured\n"
        "\n"
        "✅ All passed on the current code."
    )


def test_a_kept_set_only_a_trigger_runs_keeps_its_steps_and_effect() -> None:
    proposal = ChecksProposed(
        agent_id="a",
        task_id="7",
        steps=(ProposedStep(name="Lint", command=("ruff",)),),
        triggers={"mutation-manual": MANUAL_MUTATION},
    )
    assert proposal_text(proposal).endswith(
        "Set mutation: by hand\n"
        "  steps and whether it holds merges kept as configured\n"
        "\n"
        "✅ All passed on the current code."
    )


TESTS_REPORT = ProposedReport(set="mutation", role="tests-reviewer", prompt="Report {output}.")


def test_a_report_is_listed_under_its_set_saying_when_it_queues_its_lens() -> None:
    proposal = WITH_SET.model_copy(update={"reports": {"mutation-report": TESTS_REPORT}})
    assert proposal_text(proposal).endswith(
        "Set mutation: every 10 commits, reports\n"
        "  3. C: c\n"
        "  mutation → tests-reviewer when it fails\n"
        "\n"
        "✅ All passed on the current code."
    )


def test_a_report_on_any_status_queues_its_lens_when_the_set_finishes() -> None:
    report = TESTS_REPORT.model_copy(update={"status": "any"})
    proposal = WITH_SET.model_copy(update={"reports": {"mutation-report": report}})
    assert "  mutation → tests-reviewer when it finishes\n" in proposal_text(proposal)


def test_a_report_on_a_kept_set_groups_the_steps_and_shows_the_set() -> None:
    proposal = PROPOSAL.model_copy(update={"reports": {"mutation-report": TESTS_REPORT}})
    assert proposal_text(proposal) == (
        "Setup: none\n"
        "\n"
        "Task checks: every task\n"
        "  1. Lint: ruff\n"
        "\n"
        "Set mutation: no trigger\n"
        "  steps and whether it holds merges kept as configured\n"
        "  mutation → tests-reviewer when it fails\n"
        "\n"
        "✅ All passed on the current code."
    )
