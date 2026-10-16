"""The built-in roles' instructions, read from the Markdown files shipped with orbit."""

from importlib.resources import files

from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.protocol import BUILT_IN_ROLE_NAMES

FOLDER = files("orbit.agent.external") / "instructions"


def test_there_is_one_markdown_file_per_built_in_role() -> None:
    names = {entry.name for entry in FOLDER.iterdir() if entry.name.endswith(".md")}
    assert names == {f"{name}.md" for name in BUILT_IN_ROLE_NAMES}


def test_each_built_in_role_is_told_its_file() -> None:
    roles = read_built_in_roles()
    assert tuple(roles) == BUILT_IN_ROLE_NAMES
    for name, role in roles.items():
        assert role.instructions == (FOLDER / f"{name}.md").read_text(encoding="utf-8").strip()


def test_the_coder_leaves_committing_to_orbit() -> None:
    assert "uncommitted" in read_built_in_roles()["coder"].instructions


def test_the_review_roles_and_planner_read_but_leave_changes_to_the_coder() -> None:
    roles = read_built_in_roles()
    for name in ("reviewer", "super-reviewer", "planner"):
        assert "You can read and search the code" in roles[name].instructions


def test_the_configurator_looks_for_the_projects_own_checks() -> None:
    instructions = read_built_in_roles()["configurator"].instructions
    for hint in ("pre-commit", "package.json", "pyproject.toml", "Makefile", "quick", "setup"):
        assert hint in instructions
    assert "your work is reading and proposing" in instructions
