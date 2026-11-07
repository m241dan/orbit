"""The reference config: every setting a project can change, commented out at its default with
what it does, generated from orbit's models and the built-in roles and triggers, and committed to
the package."""

import re
import runpy
import sys
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from orbit.agent.domain.checks import Setup, VerificationSet
from orbit.agent.domain.config import (
    LIMIT_KEYS,
    SUPER_REVIEW_EVERY_MOVING,
    OrbitConfig,
    Package,
    load_layered,
)
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.domain.roles import ROLE_TABLES, Role, RoleTools, ServerGrant, old_tools_problems
from orbit.agent.domain.skills import SkillMapping
from orbit.agent.domain.triggers import Trigger
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.agent.external.built_in_triggers import read_built_in_triggers
from orbit.agent.external.reference_config import REFERENCE_WIDTH, reference_config
from orbit.protocol import BUILT_IN_ROLE_NAMES

MODULE = "orbit.agent.external.reference_config"
COMMITTED = files("orbit.agent.external") / "reference" / "config.toml"
LATER = "# Coming in a later version of orbit; leave commented out until then."
KEY_LINE = re.compile(r"# (\[|[a-z_]+ = )")
NAMED_KEY = re.compile(r"# ([a-z_]+)(?: = |: )")
EXAMPLE_HEADERS = {
    "# [[verification.steps]]",
    "# [verification.sets.mutation]",
    "# [setup]",
    "# [[skills]]",
    "# [mcp.github]",
    "# [mcp.semgrep]",
    "# [roles.license-reviewer.tools]",
}
EXAMPLE_ROLE = "license-reviewer"
BUILT_IN = read_built_in_roles()
SHIPPED = read_built_in_triggers()
PACKAGE = Package(BUILT_IN, SHIPPED)
REFERENCE = reference_config()


def lines() -> list[str]:
    """The reference's lines."""
    return REFERENCE.splitlines()


def blocks(text: str) -> list[str]:
    """``text``'s blocks, apart from each other by a blank line."""
    return text.split("\n\n")


def current() -> str:
    """The reference's settings this version of orbit has: those before the later-version
    heading."""
    return REFERENCE.partition(LATER)[0]


def later() -> str:
    """The reference's settings for a later version of orbit: those after its heading."""
    return REFERENCE.partition(LATER)[2]


def limits_roles_and_triggers() -> str:
    """The reference's blocks of limits, roles and triggers: those before the later-version
    heading but the examples of verification steps, their sets, setup, skills by path, MCP
    servers and a role's MCP servers."""
    return "\n\n".join(
        block for block in blocks(current()) if not EXAMPLE_HEADERS & set(block.splitlines())
    )


def uncommented(text: str) -> str:
    """``text`` with one leading ``# `` stripped from each key line: each table header, and
    each key with its value."""
    return "\n".join(
        line.removeprefix("# ") if KEY_LINE.match(line) else line for line in text.splitlines()
    )


def loaded(text: str) -> dict[str, Any]:
    """The TOML ``text`` is once its key lines are uncommented."""
    return tomllib.loads(uncommented(text))


def load(data: dict[str, Any]) -> OrbitConfig:
    """The configuration a project file holding ``data`` makes over the package's roles and
    triggers."""
    return load_layered(data, PACKAGE).config


def table(name: str, section: str = "roles") -> list[str]:
    """The lines of the table ``name`` in ``section``, a role's by default, from its header."""
    return next(
        block.splitlines()
        for block in blocks(REFERENCE)
        if block.startswith(f"# [{section}.{name}]")
    )


def tools_table(name: str) -> list[str]:
    """The lines of built-in role ``name``'s tools table, from the line describing it."""
    return next(
        block.splitlines()
        for block in blocks(REFERENCE)
        if f"# [roles.{name}.tools]" in block.splitlines()
    )


OWN_FIELDS = [key for key in Role.model_fields if key not in ROLE_TABLES]
"""A role's fields that aren't tables of their own, in order."""


def trigger_table(name: str) -> list[str]:
    """The lines of shipped trigger ``name``'s table, from its header."""
    return table(name, "triggers")


def named(line: str) -> str | None:
    """The key ``line`` gives a value or says is unset, if it's one that does."""
    match = NAMED_KEY.match(line)
    return match[1] if match else None


def described(key: str, table_lines: list[str]) -> str:
    """The line above ``key``'s among ``table_lines``."""
    index = next(i for i, line in enumerate(table_lines) if named(line) == key)
    return table_lines[index - 1]


def test_committed_reference_matches_the_models() -> None:
    assert COMMITTED.read_text(encoding="utf-8") == REFERENCE, (
        "src/orbit/agent/external/reference/config.toml is stale: run"
        " `uv run python -m orbit.agent.external.reference_config`"
    )


def test_starts_by_saying_what_it_is() -> None:
    assert lines()[0] == (
        "# orbit's project configuration, every setting commented out at its default."
    )
    assert lines()[1].startswith("# Uncomment the settings you change")


def test_everything_is_commented_out() -> None:
    assert tomllib.loads(REFERENCE) == {}
    assert all(line.startswith("# ") or not line for line in lines())


def test_lines_fit_in_100_columns() -> None:
    assert REFERENCE_WIDTH == 100
    assert max(map(len, lines())) <= REFERENCE_WIDTH


def test_sections_are_in_order() -> None:
    markers = [
        f"# {LIMIT_KEYS[0]} = ",
        "# [[verification.steps]]",
        "# [verification.sets.mutation]",
        "# [setup]",
        *(f"# [roles.{name}]" for name in BUILT_IN),
        f"# [roles.{EXAMPLE_ROLE}]",
        *(f"# [triggers.{name}]" for name in SHIPPED),
        "# [[skills]]",
        LATER,
    ]
    first = [next(i for i, line in enumerate(lines()) if line.startswith(m)) for m in markers]

    assert first == sorted(first)


def test_uncommented_limits_roles_and_triggers_resolve_to_the_defaults() -> None:
    data = loaded(limits_roles_and_triggers())
    config = load(data)

    assert set(LIMIT_KEYS) <= data.keys()
    assert list(data["roles"]) == [*BUILT_IN, EXAMPLE_ROLE]
    assert list(data["triggers"]) == list(SHIPPED)
    built_ins = {name: role for name, role in config.roles.items() if name != EXAMPLE_ROLE}
    assert built_ins == BUILT_IN
    assert config.triggers == SHIPPED
    assert config.notices == (SUPER_REVIEW_EVERY_MOVING,)
    update = {"roles": built_ins, "notices": ()}
    assert config.model_copy(update=update) == load({})


def test_uncommented_built_in_tables_set_each_field_with_a_value() -> None:
    roles = loaded(limits_roles_and_triggers())["roles"]

    assert set(roles["coder"]) == set(Role.model_fields) - {
        "instructions",
        "model",
        "timeout_minutes",
        "effort",
    }
    assert roles["drift-reviewer"]["model"] == "opus"


def test_uncommented_example_role_is_a_license_reviewer() -> None:
    config = load(loaded(limits_roles_and_triggers()))

    assert config.roles[EXAMPLE_ROLE] == Role(
        instructions="roles/license-reviewer.md",
        lane="licenses",
        workspace="snapshot",
        merge=False,
        verify=False,
    )


def test_uncommented_examples_set_a_step_in_a_set_its_table_and_a_setup() -> None:
    config = load(loaded(current()))

    mutation = VerificationStep("Mutation tests", ("uv", "run", "mutmut", "run"), set="mutation")
    assert config.verification.steps == (mutation,)
    assert config.verification.sets == {"mutation": VerificationSet(holds_merges=True)}
    assert config.setup == Setup(command=("uv", "sync"), watch=("uv.lock",))


def test_uncommented_mcp_examples_declare_two_servers_a_role_uses() -> None:
    config = load(loaded(current()))

    assert config.mcp == {
        "github": McpServer(
            command=("github-mcp-server", "stdio"),
            env={"GITHUB_TOKEN": "$GITHUB_TOKEN"},
            approve=("create_issue",),
        ),
        "semgrep": McpServer(
            url="http://127.0.0.1:3010/mcp", headers={"Authorization": "Bearer ${SEMGREP_TOKEN}"}
        ),
    }
    assert config.roles[EXAMPLE_ROLE].tools.mcp == (
        ServerGrant(server="semgrep"),
        ServerGrant(server="github", tools=("get_issue", "list_issues", "create_issue")),
    )


def test_the_mcp_examples_follow_the_skills_example_before_the_later_heading() -> None:
    markers = ["# [[skills]]", "# [mcp.github]", "# [mcp.semgrep]", LATER]
    first = [next(i for i, line in enumerate(lines()) if line.startswith(m)) for m in markers]

    assert first == sorted(first)


def test_the_step_example_shows_its_set_under_its_description() -> None:
    example = next(block for block in blocks(REFERENCE) if "# [[verification.steps]]" in block)

    assert example.splitlines()[-2:] == [
        "# The named set it's in; left out, it's one of the task checks every task runs.",
        '# set = "mutation"',
    ]


def test_the_sets_example_shows_whether_it_holds_merges() -> None:
    example = next(block for block in blocks(REFERENCE) if "# [verification.sets." in block)

    assert example.splitlines() == [
        '# The named sets steps are in, by name; a trigger runs one with checks = "<name>". For'
        " example:",
        "# [verification.sets.mutation]",
        "# Whether its failure holds merges until a fix lands; otherwise it only reports.",
        "# holds_merges = true",
    ]


def test_uncommented_skills_example_maps_src_to_two_skills() -> None:
    config = load(loaded(current()))

    assert config.skills == (SkillMapping(paths=("src",), use=("application", "python")),)


def test_skills_example_sets_each_key_under_its_description() -> None:
    example = next(block for block in blocks(REFERENCE) if "# [[skills]]" in block.splitlines())

    assert example.splitlines() == [
        "# Skills for the code under some paths, given to every agent along with its role's."
        " For example:",
        "# [[skills]]",
        "# Files or folders in the project, relative to its root; each must exist.",
        '# paths = ["src"]',
        "# The skills that apply there, by name.",
        '# use = ["application", "python"]',
    ]


def test_limits_come_in_order_each_under_its_description() -> None:
    keys = [key for line in lines() if (key := named(line))]

    assert keys[: len(LIMIT_KEYS)] == list(LIMIT_KEYS)
    for key in LIMIT_KEYS:
        assert described(key, lines()) == f"# {OrbitConfig.model_fields[key].description}"


@pytest.mark.parametrize("name", BUILT_IN_ROLE_NAMES)
def test_each_built_in_table_lists_every_role_field_but_its_tools(name: str) -> None:
    keys = [key for line in table(name) if (key := named(line))]

    assert keys == OWN_FIELDS


@pytest.mark.parametrize("name", BUILT_IN_ROLE_NAMES)
def test_each_built_in_roles_tools_table_follows_it_under_its_description(name: str) -> None:
    tools = tools_table(name)
    following = blocks(REFERENCE)[blocks(REFERENCE).index("\n".join(table(name))) + 1]

    assert following == "\n".join(tools)
    assert tools[:2] == [f"# {Role.model_fields['tools'].description}", f"# [roles.{name}.tools]"]
    assert [key for line in tools if (key := named(line))] == list(RoleTools.model_fields)


def test_each_role_field_is_under_its_description() -> None:
    for key in OWN_FIELDS:
        assert described(key, table("coder")) == f"# {Role.model_fields[key].description}"
    for key, field in RoleTools.model_fields.items():
        assert described(key, tools_table("coder")) == f"# {field.description}"


def test_built_in_tables_show_shipped_values() -> None:
    coder = table("coder")

    assert '# workspace = "shell"' in tools_table("coder")
    assert '# workspace = "read"' in tools_table("planner")
    assert "# web = true" in tools_table("coder")
    assert '# orbit = ["get_task", "complete_task", "run_check", "report_progress", "give_up"]' in (
        tools_table("coder")
    )
    assert "# merge = true" in coder
    assert '# runtime = "claude"' in coder
    assert '# model = "opus"' in table("drift-reviewer")
    assert '# workspace = "checkout"' in table("planner")


def test_a_built_in_roles_skills_show_as_shipped() -> None:
    assert '# skills = ["code-style"]' in table("planner")
    assert '# skills = ["code-style", "security"]' in table("coder")
    assert "# skills = []" in table("experimenter")


def test_the_skills_field_says_whose_skills_a_role_names() -> None:
    assert described("skills", table("coder")) == (
        "# The skills its agents get, by name: orbit's own, or the project's in .orbit/skills/."
    )


def test_a_list_too_long_for_a_line_says_how_many_and_where_to_see_them() -> None:
    reviewer = table("reviewer")

    assert "# skills: 11 of orbit's own; orbit config show roles.reviewer.skills lists them" in (
        reviewer
    )
    assert described("skills", reviewer) == f"# {Role.model_fields['skills'].description}"
    assert (
        "# orbit: 6 of orbit's own; orbit config show roles.reviewer.tools.orbit lists them"
        in tools_table("reviewer")
    )


def test_unset_fields_say_so() -> None:
    coder = table("coder")

    assert {"# model: unset", "# timeout_minutes: unset", "# effort: unset"} <= set(coder)


@pytest.mark.parametrize("key", ["model", "effort", "runtime"])
def test_machine_fields_say_the_user_config_sets_them(key: str) -> None:
    assert "the user config's" in described(key, table("coder"))


def test_built_in_instructions_name_the_file_shipped_with_orbit() -> None:
    assert "# instructions: orbit's own coder.md" in table("coder")
    assert "# instructions: orbit's own super-lens-reviewer.md" in table("drift-reviewer")


def test_the_triggers_follow_the_line_introducing_them() -> None:
    heading = f"# {OrbitConfig.model_fields['triggers'].description}"
    following = blocks(REFERENCE)[blocks(REFERENCE).index(heading) + 1]
    assert following.startswith(f"# [triggers.{next(iter(SHIPPED))}]")


@pytest.mark.parametrize("name", list(SHIPPED))
def test_each_shipped_trigger_table_lists_every_trigger_field(name: str) -> None:
    keys = [key for line in trigger_table(name) if (key := named(line))]

    assert keys == list(Trigger.model_fields)


def test_each_trigger_field_is_under_its_description() -> None:
    for key, field in Trigger.model_fields.items():
        assert described(key, trigger_table("super-review")) == f"# {field.description}"


def test_shipped_trigger_tables_show_shipped_values() -> None:
    review, super_review = trigger_table("bugs-review"), trigger_table("super-review")

    assert '# on = "task_landed"' in review
    assert '# guards = ["review-rounds", "code-changed"]' in review
    assert "# when = {}" in review
    assert '# when = {origin = ["manual", "planner"]}' in super_review
    assert "# every = 10" in super_review
    assert {"# manual = true", '# label = "Super review"'} <= set(super_review)
    assert '# role = ["style-reviewer", "drift-reviewer", "scaling-reviewer"]' in super_review
    assert "# draft = true" in trigger_table("startup-fix")
    assert '# when = {findings = "some", review = ["commit", "batch", "set"]}' in trigger_table(
        "merge-pass"
    )
    assert '# when = {findings = "some", review = "super"}' in trigger_table("super-merge-pass")
    assert '# tab = "super-review"' in trigger_table("super-merge-pass")


def test_shipped_prompts_name_the_template_shipped_with_orbit() -> None:
    assert "# prompt: orbit's own lens-review.md" in trigger_table("bugs-review")
    assert "# prompt: orbit's own merge-pass.md" in trigger_table("merge-pass")
    assert "# prompt: orbit's own configure-checks.md" in trigger_table("set-up-verification")


def test_a_shipped_triggers_unset_fields_say_so() -> None:
    check = trigger_table("startup-check")

    assert {"# role: unset", "# prompt: unset", "# every: unset", "# label: unset"} <= set(check)
    assert '# checks = "task-checks"' in check


def test_every_key_is_under_a_line_explaining_it() -> None:
    all_lines = lines()
    for index, line in enumerate(all_lines):
        if named(line):
            above = all_lines[index - 1]
            assert above.startswith("# ") and not named(above), line
            assert not above.startswith("# ["), line


def test_later_version_settings_follow_their_heading_commented_out() -> None:
    settings = later()

    assert "# [models." in settings
    assert "triggers" not in settings
    assert "mcp" not in settings
    assert "roles" not in settings
    assert all(line.startswith("# ") for line in settings.splitlines() if line)


def test_a_roles_runtime_is_among_its_fields_rather_than_a_later_versions() -> None:
    assert "runtime" not in later()
    assert '# runtime = "claude"' in table("planner")


OLD_TOOLS_LINE = re.compile(r"^(# )?(tool_access = |tools = \[)", re.MULTILINE)
"""A line, set or commented out, giving a role's tools the old way."""
ROOT = Path(__file__).parent.parent
STANDING_CONFIGS = (
    COMMITTED,
    files("orbit.agent.external") / "instructions" / "roles.toml",
    ROOT / ".orbit" / "config.toml",
    ROOT / "README.md",
)
"""The configs orbit ships or keeps that give roles' tools: the reference, the built-in roles,
orbit's own file, and the README's examples."""


@pytest.mark.parametrize("path", STANDING_CONFIGS, ids=lambda path: path.name)
def test_no_standing_config_gives_a_roles_tools_the_old_way(path: Path) -> None:
    assert not OLD_TOOLS_LINE.search(path.read_text(encoding="utf-8"))


def test_the_uncommented_reference_and_the_built_in_roles_give_tools_only_as_tables() -> None:
    shipped = tomllib.loads(STANDING_CONFIGS[1].read_text(encoding="utf-8"))["roles"]
    reference = loaded(current() + later())["roles"]

    assert old_tools_problems(shipped) == old_tools_problems(reference) == []
    assert all(isinstance(fields["tools"], dict) for fields in shipped.values())


def test_later_version_settings_are_valid_toml_uncommented() -> None:
    data = loaded(current() + later())

    assert "models" in data
    assert "mcp" not in loaded(later())


def test_module_writes_the_reference_to_the_given_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "out" / "config.toml"
    monkeypatch.setattr("sys.argv", ["reference_config", str(path)])
    monkeypatch.delitem(sys.modules, MODULE)

    runpy.run_module(MODULE, run_name="__main__")

    assert path.read_text(encoding="utf-8") == REFERENCE


def test_module_writes_the_packages_copy_under_the_current_directory_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["reference_config"])
    monkeypatch.delitem(sys.modules, MODULE)

    runpy.run_module(MODULE, run_name="__main__")

    path = tmp_path / "src" / "orbit" / "agent" / "external" / "reference" / "config.toml"
    assert path.read_text(encoding="utf-8") == REFERENCE
