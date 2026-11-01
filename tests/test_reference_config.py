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

from orbit.agent.domain.checks import Setup
from orbit.agent.domain.config import (
    LIMIT_KEYS,
    SUPER_REVIEW_EVERY_MOVING,
    OrbitConfig,
    Package,
    load_layered,
)
from orbit.agent.domain.roles import Role
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
EXAMPLE_HEADERS = {"# [[verification.steps]]", "# [setup]", "# [[skills]]"}
EXAMPLE_ROLE = "security-reviewer"
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
    heading but the examples of verification steps, setup and skills by path."""
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
    assert roles["super-reviewer"]["model"] == "opus"


def test_uncommented_example_role_is_a_security_reviewer() -> None:
    config = load(loaded(limits_roles_and_triggers()))

    assert config.roles[EXAMPLE_ROLE] == Role(
        instructions="roles/security-reviewer.md",
        lane="security",
        workspace="snapshot",
        merge=False,
        verify=False,
    )


def test_uncommented_examples_set_a_step_and_a_setup() -> None:
    config = load(loaded(current()))

    assert config.verification.steps == (VerificationStep("Tests", ("uv", "run", "pytest")),)
    assert config.setup == Setup(command=("uv", "sync"), watch=("uv.lock",))


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
def test_each_built_in_table_lists_every_role_field(name: str) -> None:
    keys = [key for line in table(name) if (key := named(line))]

    assert keys == list(Role.model_fields)


def test_each_role_field_is_under_its_description() -> None:
    for key, field in Role.model_fields.items():
        assert described(key, table("coder")) == f"# {field.description}"


def test_built_in_tables_show_shipped_values() -> None:
    coder = table("coder")

    assert '# tool_access = "full"' in coder
    assert '# tools = ["get_task", "complete_task", "run_check", "report_progress", "give_up"]' in (
        coder
    )
    assert "# merge = true" in coder
    assert '# runtime = "claude"' in coder
    assert '# model = "opus"' in table("super-reviewer")
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

    assert "# skills: 8 of orbit's own; orbit config show roles.reviewer.skills lists them" in (
        reviewer
    )
    assert described("skills", reviewer) == f"# {Role.model_fields['skills'].description}"


def test_unset_fields_say_so() -> None:
    coder = table("coder")

    assert {"# model: unset", "# timeout_minutes: unset", "# effort: unset"} <= set(coder)


@pytest.mark.parametrize("key", ["model", "effort", "runtime"])
def test_machine_fields_say_the_user_config_sets_them(key: str) -> None:
    assert "the user config's" in described(key, table("coder"))


def test_built_in_instructions_name_the_file_shipped_with_orbit() -> None:
    assert "# instructions: orbit's own coder.md" in table("coder")
    assert "# instructions: orbit's own super-reviewer.md" in table("super-reviewer")


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
    review, super_review = trigger_table("review"), trigger_table("super-review")

    assert '# on = "task_landed"' in review
    assert '# guards = ["review-rounds"]' in review
    assert "# when = {}" in review
    assert '# when = {origin = ["manual", "planner"]}' in super_review
    assert "# every = 10" in super_review
    assert {"# manual = true", '# label = "Super review"'} <= set(super_review)
    assert "# draft = true" in trigger_table("startup-fix")


def test_shipped_prompts_name_the_template_shipped_with_orbit() -> None:
    assert "# prompt: orbit's own review.md" in trigger_table("review")
    assert "# prompt: orbit's own configure-checks.md" in trigger_table("set-up-verification")


def test_a_shipped_triggers_unset_fields_say_so() -> None:
    check = trigger_table("startup-check")

    assert {"# role: unset", "# prompt: unset", "# every: unset", "# label: unset"} <= set(check)
    assert '# checks = "gate"' in check


def test_every_key_is_under_a_line_explaining_it() -> None:
    all_lines = lines()
    for index, line in enumerate(all_lines):
        if named(line):
            above = all_lines[index - 1]
            assert above.startswith("# ") and not named(above), line
            assert not above.startswith("# ["), line


def test_later_version_settings_follow_their_heading_commented_out() -> None:
    settings = later()

    for section in ("# [mcp.", "# [models."):
        assert section in settings
    assert "triggers" not in settings
    assert re.search(r"^# \[roles\.[a-z-]+\.tools\]$", settings, re.MULTILINE)
    assert all(line.startswith("# ") for line in settings.splitlines() if line)


def test_a_roles_runtime_is_among_its_fields_rather_than_a_later_versions() -> None:
    assert "runtime" not in later()
    assert '# runtime = "claude"' in table("planner")


def test_later_version_settings_are_valid_toml_uncommented() -> None:
    data = loaded(current() + later())

    assert {"mcp", "models"} <= data.keys()
    assert "tools" in data["roles"][EXAMPLE_ROLE]


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
