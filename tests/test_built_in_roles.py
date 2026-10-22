"""The built-in roles' instructions, read from the Markdown files shipped with orbit."""

import re
from importlib.resources import files
from pathlib import Path

from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.protocol import BUILT_IN_ROLE_NAMES

FOLDER = files("orbit.agent.external") / "instructions"
README = Path(__file__).parent.parent / "README.md"


def readme_tools(name: str) -> tuple[str, ...]:
    """The tools README.md's built-in roles table lists for the role ``name``."""
    row = next(
        line
        for line in README.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"| `{name}` |")
    )
    return tuple(re.findall(r"`([^`]+)`", row.split("|")[2]))


def test_there_is_one_markdown_file_per_built_in_role() -> None:
    names = {entry.name for entry in FOLDER.iterdir() if entry.name.endswith(".md")}
    assert names == {f"{name}.md" for name in BUILT_IN_ROLE_NAMES}


def test_each_built_in_role_is_told_its_file() -> None:
    roles = read_built_in_roles()
    assert tuple(roles) == BUILT_IN_ROLE_NAMES
    for name, role in roles.items():
        assert role.instructions == (FOLDER / f"{name}.md").read_text(encoding="utf-8").strip()


def test_the_coder_leaves_committing_to_orbit() -> None:
    assert "nothing staged or committed" in read_built_in_roles()["coder"].instructions


def test_the_review_roles_and_planner_read_but_leave_changes_to_the_coder() -> None:
    roles = read_built_in_roles()
    for name in ("reviewer", "super-reviewer", "planner"):
        assert "read and search the code" in roles[name].instructions


def test_the_configurator_looks_for_the_projects_own_checks() -> None:
    instructions = read_built_in_roles()["configurator"].instructions
    for hint in ("pre-commit", "package.json", "pyproject.toml", "Makefile", "quick", "setup"):
        assert hint in instructions
    assert "your work is reading and proposing" in instructions


def test_the_readme_lists_each_built_in_roles_tools() -> None:
    for name, role in read_built_in_roles().items():
        assert readme_tools(name) == role.tools, name


PLANNER_ASKS_BEFORE_PROPOSING = (
    "Before posting the requirements, read the code each likely task touches and walk each task"
    " as its coder would: the states it passes through, what happens before and after it, what"
    " fails and what a test would pin down. Any behaviour a prompt would have to state that no"
    " answer settles is still open: ask it in a last round.",
    "Mark a line as following only when it follows from an answer, an earlier record or the"
    " code; a choice of your own is a question.",
    "A prompt carries only confirmed decisions and facts from the code. A choice that turns up"
    " while writing one goes back to the user as a question before anything is proposed: never"
    " settle it in a draft and mention it afterwards.",
    "Check that no prompt's Keep forbids what its Done requires.",
    "A task that would change most of a layer's files at once is too big: split it into steps"
    " that each leave the checks passing.",
)


def planner_text() -> str:
    """The planner's instructions with every run of whitespace made a single space."""
    return " ".join(read_built_in_roles()["planner"].instructions.split())


def test_the_planner_asks_every_open_choice_before_proposing() -> None:
    text = planner_text()
    for passage in PLANNER_ASKS_BEFORE_PROPOSING:
        assert passage in text, passage


def test_the_planner_walks_each_task_before_declaring_the_frontier_empty() -> None:
    text = planner_text()
    walk = text.index(PLANNER_ASKS_BEFORE_PROPOSING[0])
    assert text.index("## 1. Interrogate") < walk
    assert walk < text.index("A fully specified goal has an empty frontier")


def test_the_planner_keeps_its_own_choices_out_of_prompts_while_proposing() -> None:
    text = planner_text()
    carries = text.index(PLANNER_ASKS_BEFORE_PROPOSING[2])
    assert text.index("## 3. Propose") < carries < text.index("Proposals are drafts")
