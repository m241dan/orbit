"""The built-in roles, read from the roles.toml and Markdown files shipped with orbit."""

import re
import tomllib
from collections.abc import Collection
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.external.built_in_roles import read_built_in_roles, read_instruction_files
from orbit.protocol import BUILT_IN_ROLE_NAMES

FOLDER = files("orbit.agent.external") / "instructions"
README = Path(__file__).parent.parent / "README.md"
ROLES_TOML = (FOLDER / "roles.toml").read_text(encoding="utf-8")

REVIEW_TOOLS = ("get_task", "complete_task", "report_progress", "give_up", "suggest_task")
BUILT_IN_FIELDS: dict[str, dict[str, Any]] = {
    "coder": {
        "model": None,
        "tool_access": "full",
        "tools": ("get_task", "complete_task", "run_check", "report_progress", "give_up"),
        "verify": True,
        "timeout_minutes": None,
        "lane": "work",
        "workspace": "worktree",
        "conversation": False,
        "merge": True,
        "enabled": True,
        "effort": None,
        "runtime": "claude",
    },
    "reviewer": {
        "model": None,
        "tool_access": "restricted",
        "tools": REVIEW_TOOLS,
        "verify": False,
        "timeout_minutes": None,
        "lane": "review",
        "workspace": "snapshot",
        "conversation": False,
        "merge": False,
        "enabled": True,
        "effort": None,
        "runtime": "claude",
    },
    "super-reviewer": {
        "model": "opus",
        "tool_access": "restricted",
        "tools": REVIEW_TOOLS,
        "verify": False,
        "timeout_minutes": None,
        "lane": "review",
        "workspace": "snapshot",
        "conversation": False,
        "merge": False,
        "enabled": True,
        "effort": None,
        "runtime": "claude",
    },
    "planner": {
        "model": None,
        "tool_access": "restricted",
        "tools": ("propose_task", "report_progress"),
        "verify": False,
        "timeout_minutes": None,
        "lane": "planner",
        "workspace": "checkout",
        "conversation": True,
        "merge": False,
        "enabled": True,
        "effort": None,
        "runtime": "claude",
    },
    "configurator": {
        "model": None,
        "tool_access": "restricted",
        "tools": ("get_task", "propose_checks", "report_progress", "give_up"),
        "verify": False,
        "timeout_minutes": None,
        "lane": "work",
        "workspace": "worktree",
        "conversation": False,
        "merge": False,
        "enabled": True,
        "effort": None,
        "runtime": "claude",
    },
}
"""Each built-in role's fields but its instructions: those orbit has always had, where it
works, which lane runs it, whether you talk to it, whether its work merges, and no effort."""


def role_directory(tmp_path: Path, roles_toml: str, leave_out: Collection[str] = ()) -> Path:
    """``tmp_path`` holding ``roles_toml`` and the shipped Markdown files but those named in
    ``leave_out``."""
    (tmp_path / "roles.toml").write_text(roles_toml, encoding="utf-8")
    for name in BUILT_IN_ROLE_NAMES:
        if f"{name}.md" not in leave_out:
            text = (FOLDER / f"{name}.md").read_text(encoding="utf-8")
            (tmp_path / f"{name}.md").write_text(text, encoding="utf-8")
    return tmp_path


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


def test_roles_toml_ships_in_the_package_naming_each_roles_markdown_file() -> None:
    tables = tomllib.loads(ROLES_TOML)["roles"]
    assert set(tables) == set(BUILT_IN_ROLE_NAMES)
    for name, table in tables.items():
        assert table["instructions"] == f"{name}.md"


def test_roles_toml_sets_each_roles_lane_workspace_conversation_merging_and_enabling() -> None:
    for name, table in tomllib.loads(ROLES_TOML)["roles"].items():
        assert {"lane", "workspace", "conversation", "merge", "enabled"} <= set(table), name


@pytest.mark.parametrize("name", BUILT_IN_ROLE_NAMES)
def test_each_built_in_role_has_its_fields(name: str) -> None:
    role = read_built_in_roles()[name]
    assert role.model_dump(exclude={"instructions"}) == BUILT_IN_FIELDS[name]


def test_the_built_in_roles_are_read_from_the_directory_given(tmp_path: Path) -> None:
    roles_toml = ROLES_TOML.replace('model = "opus"', 'model = "haiku"')
    roles = read_built_in_roles(role_directory(tmp_path, roles_toml))
    assert roles["super-reviewer"].model == "haiku"
    assert roles["coder"] == read_built_in_roles()["coder"]


def test_each_built_in_roles_instructions_are_in_the_file_roles_toml_names() -> None:
    assert read_instruction_files() == {name: f"{name}.md" for name in BUILT_IN_ROLE_NAMES}


def test_the_instructions_files_are_read_from_the_directory_given(tmp_path: Path) -> None:
    roles_toml = ROLES_TOML.replace('"coder.md"', '"coding.md"')
    files = read_instruction_files(role_directory(tmp_path, roles_toml))
    assert (files["coder"], files["reviewer"]) == ("coding.md", "reviewer.md")


def test_an_unknown_field_in_a_role_raises_config_error_naming_it(tmp_path: Path) -> None:
    roles_toml = ROLES_TOML.replace("[roles.planner]\n", "[roles.planner]\ncolour = 'blue'\n")
    directory = role_directory(tmp_path, roles_toml)
    roles_file = re.escape(str(directory / "roles.toml"))
    with pytest.raises(ConfigError, match=f"^{roles_file}: roles\\.planner\\.colour: "):
        read_built_in_roles(directory)


def test_a_missing_markdown_file_raises_config_error_naming_it(tmp_path: Path) -> None:
    directory = role_directory(tmp_path, ROLES_TOML, leave_out={"reviewer.md"})
    markdown_file = re.escape(str(directory / "reviewer.md"))
    with pytest.raises(ConfigError, match=f"^{markdown_file}: "):
        read_built_in_roles(directory)


@pytest.mark.parametrize("roles_toml", [None, "[roles\n", "roles = 'coder'\n"])
def test_a_missing_or_malformed_roles_toml_raises_config_error_naming_it(
    tmp_path: Path, roles_toml: str | None
) -> None:
    if roles_toml is not None:
        (tmp_path / "roles.toml").write_text(roles_toml, encoding="utf-8")
    with pytest.raises(ConfigError, match=f"^{re.escape(str(tmp_path / 'roles.toml'))}: "):
        read_built_in_roles(tmp_path)


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
