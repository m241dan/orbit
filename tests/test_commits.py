from orbit.agent.domain.commits import commit_message
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
