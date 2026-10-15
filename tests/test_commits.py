from orbit.agent.domain.commits import commit_message, configure_message
from orbit.agent.domain.roles import ChecksProposal, Setup
from orbit.agent.domain.verification import VerificationStep, fix_prompt
from orbit.protocol import TaskSpec


def test_the_first_summary_is_the_subject_and_the_task_the_body() -> None:
    message = commit_message(TaskSpec(prompt="Make it faster.\nMuch faster."), ["Add --fast\nmore"])
    assert message == "Add --fast\n\nOrbit task:\n\nMake it faster.\nMuch faster."


def test_later_summaries_are_listed_as_fixes() -> None:
    message = commit_message(TaskSpec(prompt="Go"), ["Add --fast", "Fix the test", "Format"])
    assert message == (
        "Add --fast\n\nOrbit task:\n\nGo\n\nFixed after verification:\n- Fix the test\n- Format"
    )


def test_without_a_summary_the_task_titles_the_commit() -> None:
    assert commit_message(TaskSpec(prompt="Make it faster.\nMore."), []).startswith(
        "Make it faster.\n\n"
    )
    assert commit_message(TaskSpec(prompt="Tidy up"), ["  "]).startswith("Tidy up\n\n")


def test_long_subjects_and_prompts_are_trimmed() -> None:
    message = commit_message(TaskSpec(prompt="p" * 3000), ["s" * 100])
    subject, _, body = message.partition("\n\n")
    assert subject == "s" * 71 + "…"
    assert body.endswith("p" * 2000 + "…")


def test_a_fix_tasks_body_is_the_step_and_command_without_the_output() -> None:
    prompt = fix_prompt(VerificationStep("Lint", ("ruff", "check")), "E501 line too long\n")
    message = commit_message(TaskSpec(prompt=prompt), ["Wrap the line"], fix=True)
    assert message == (
        "Wrap the line\n\nOrbit task:\n\nFix the failed verification step: Lint\n\n"
        "This command failed and must pass:\n    ruff check"
    )


def test_accepted_checks_are_committed_with_each_step_listed() -> None:
    checks = ChecksProposal(
        steps=(
            VerificationStep("Lint", ("ruff", "check")),
            VerificationStep("Tests", ("pytest", "-k", "not slow")),
        ),
        setup=Setup(command=("uv", "sync"), watch=("uv.lock", "pyproject.toml")),
    )
    assert configure_message(checks) == (
        "Configure verification steps\n\n"
        "Steps:\n- Lint: ruff check\n- Tests: pytest -k 'not slow'\n\n"
        "Setup: uv sync (again when uv.lock, pyproject.toml changes)"
    )


def test_a_setup_without_watched_files_is_listed_alone_and_none_is_left_out() -> None:
    steps = (VerificationStep("Lint", ("ruff",)),)
    with_setup = ChecksProposal(steps=steps, setup=Setup(command=("make",)))
    assert configure_message(with_setup).endswith("- Lint: ruff\n\nSetup: make")
    assert configure_message(ChecksProposal(steps=steps)).endswith("- Lint: ruff")
