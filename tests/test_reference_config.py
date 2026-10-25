"""The reference config: every setting a project can change, commented out at its default with
what it does, generated from orbit's models and the built-in roles, and committed to the
package."""

import re
import runpy
import sys
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from orbit.agent.domain.checks import Setup
from orbit.agent.domain.config import LIMIT_KEYS, OrbitConfig, load_config
from orbit.agent.domain.roles import Role
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.agent.external.reference_config import reference_config
from orbit.protocol import BUILT_IN_ROLE_NAMES

MODULE = "orbit.agent.external.reference_config"
COMMITTED = files("orbit.agent.external") / "reference" / "config.toml"
LATER = "# Coming in a later version of orbit; leave commented out until then."
KEY_LINE = re.compile(r"# (\[|[a-z_]+ = )")
NAMED_KEY = re.compile(r"# ([a-z_]+)(?: = |: )")
EXAMPLE_HEADERS = {"# [[verification.steps]]", "# [setup]"}
EXAMPLE_ROLE = "security-reviewer"
BUILT_IN = read_built_in_roles()
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


def limits_and_roles() -> str:
    """The reference's blocks of limits and roles: those before the later-version heading but
    the examples of verification steps and setup."""
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


def table(name: str) -> list[str]:
    """The lines of role ``name``'s table, from its header."""
    return next(
        block.splitlines() for block in blocks(REFERENCE) if block.startswith(f"# [roles.{name}]")
    )


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
    assert max(map(len, lines())) <= 100


def test_sections_are_in_order() -> None:
    markers = [
        f"# {LIMIT_KEYS[0]} = ",
        "# [[verification.steps]]",
        "# [setup]",
        *(f"# [roles.{name}]" for name in BUILT_IN),
        f"# [roles.{EXAMPLE_ROLE}]",
        LATER,
    ]
    first = [next(i for i, line in enumerate(lines()) if line.startswith(m)) for m in markers]

    assert first == sorted(first)


def test_uncommented_limits_and_roles_resolve_to_the_defaults() -> None:
    data = loaded(limits_and_roles())
    config = load_config(data, BUILT_IN)

    assert set(LIMIT_KEYS) <= data.keys()
    assert list(data["roles"]) == [*BUILT_IN, EXAMPLE_ROLE]
    built_ins = {name: role for name, role in config.roles.items() if name != EXAMPLE_ROLE}
    assert built_ins == BUILT_IN
    assert config.model_copy(update={"roles": built_ins}) == load_config({}, BUILT_IN)


def test_uncommented_built_in_tables_set_each_field_with_a_value() -> None:
    roles = loaded(limits_and_roles())["roles"]

    assert set(roles["coder"]) == set(Role.model_fields) - {
        "instructions",
        "model",
        "timeout_minutes",
        "effort",
    }
    assert roles["super-reviewer"]["model"] == "opus"


def test_uncommented_example_role_is_a_security_reviewer() -> None:
    config = load_config(loaded(limits_and_roles()), BUILT_IN)

    assert config.roles[EXAMPLE_ROLE] == Role(
        instructions="roles/security-reviewer.md",
        lane="security",
        workspace="snapshot",
        merge=False,
        verify=False,
    )


def test_uncommented_examples_set_a_step_and_a_setup() -> None:
    config = load_config(loaded(current()), BUILT_IN)

    assert config.verification.steps == (VerificationStep("Tests", ("uv", "run", "pytest")),)
    assert config.setup == Setup(command=("uv", "sync"), watch=("uv.lock",))


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
    assert '# model = "opus"' in table("super-reviewer")
    assert '# workspace = "checkout"' in table("planner")


def test_unset_fields_say_so() -> None:
    coder = table("coder")

    assert {"# model: unset", "# timeout_minutes: unset", "# effort: unset"} <= set(coder)


def test_built_in_instructions_name_the_file_shipped_with_orbit() -> None:
    assert "# instructions: orbit's own coder.md" in table("coder")
    assert "# instructions: orbit's own super-reviewer.md" in table("super-reviewer")


def test_every_key_is_under_a_line_explaining_it() -> None:
    all_lines = lines()
    for index, line in enumerate(all_lines):
        if named(line):
            above = all_lines[index - 1]
            assert above.startswith("# ") and not named(above), line
            assert not above.startswith("# ["), line


def test_later_version_settings_follow_their_heading_commented_out() -> None:
    settings = later()

    for section in ("# [triggers.", "# [mcp.", "# [models."):
        assert section in settings
    assert re.search(r"^# \[roles\.[a-z-]+\.tools\]$", settings, re.MULTILINE)
    assert re.search(r"^# runtime = ", settings, re.MULTILINE)
    assert all(line.startswith("# ") for line in settings.splitlines() if line)


def test_later_version_settings_are_valid_toml_uncommented() -> None:
    data = loaded(current() + later())

    assert data["roles"][EXAMPLE_ROLE]["runtime"] == "claude"
    assert {"triggers", "mcp", "models"} <= data.keys()
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
