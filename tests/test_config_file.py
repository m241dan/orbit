import os
import re
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from orbit.agent.application.ports import ConfigWriteFailed
from orbit.agent.domain.checks import ChecksProposal, Setup
from orbit.agent.domain.config import OrbitConfig
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.config_report import Written
from orbit.agent.domain.config_update import Additions, ConfigUpdate, update_text
from orbit.agent.domain.layers import Layer
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.agent.external.config_file import (
    CONFIG_PATH,
    REFERENCE,
    read_config,
    read_config_sources,
    write_checks,
)
from orbit.agent.external.role_instructions import resolve_instructions
from orbit.protocol import BUILT_IN_ROLE_NAMES, PLANNER

BUILT_IN_ROLES = read_built_in_roles()


def write(root: Path, text: str) -> Path:
    path = root / CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_without_a_file_the_defaults_apply(tmp_path: Path) -> None:
    assert read_config(str(tmp_path), tmp_path / "config") == OrbitConfig(roles=BUILT_IN_ROLES)


def test_the_file_is_read_over_the_defaults(tmp_path: Path) -> None:
    write(
        tmp_path,
        """
fix_attempts = 3

[roles.coder]
model = "sonnet"
timeout_minutes = 45
""",
    )
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.fix_attempts == 3
    assert config.roles["coder"].model == "sonnet"
    assert config.roles["reviewer"] == BUILT_IN_ROLES["reviewer"]


def write_user_config(config_dir: Path, text: str) -> Path:
    path = config_dir / "config.toml"
    path.parent.mkdir()
    path.write_text(text)
    return path


def test_the_user_configs_role_fields_are_read_beneath_the_files(
    tmp_path: Path, config_dir: Path
) -> None:
    write_user_config(config_dir, '[roles]\neffort = "low"\n\n[roles.planner]\nmodel = "opus"\n')
    planner = read_config(str(tmp_path), config_dir).roles[PLANNER]
    assert (planner.model, planner.effort) == ("opus", "low")
    write(tmp_path, '[roles.planner]\nmodel = "sonnet"\n')
    planner = read_config(str(tmp_path), config_dir).roles[PLANNER]
    assert (planner.model, planner.effort) == ("sonnet", "low")


def test_a_user_config_without_roles_leaves_them_as_they_are(
    tmp_path: Path, config_dir: Path
) -> None:
    write_user_config(config_dir, "[storage]\ntool_output_kb = 1\n")
    assert read_config(str(tmp_path), config_dir) == OrbitConfig(roles=BUILT_IN_ROLES)


def test_a_role_field_the_user_config_cant_set_names_it_and_both_files(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write_user_config(config_dir, "[roles.coder]\nverify = false\n")
    write(tmp_path, "fix_attempts = 2\n")
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value).startswith(
        f"{path}: roles.coder.verify: belongs in the project's .orbit/config.toml"
    )


HELPER = """
[roles.helper]
instructions = "roles/helper.md"
lane = "helper"
workspace = "snapshot"
merge = false
verify = false
"""
"""A personal role whose instructions are in a file under the config directory."""


def write_helper(config_dir: Path, text: str = HELPER) -> Path:
    """The user config in ``config_dir`` defining the personal role ``helper`` as ``text``
    says, with its instructions file."""
    path = write_user_config(config_dir, text)
    (config_dir / "roles").mkdir()
    (config_dir / "roles" / "helper.md").write_text("You help.\n", encoding="utf-8")
    return path


def test_a_personal_role_is_read_into_every_workspace_marked_personal(
    tmp_path: Path, config_dir: Path
) -> None:
    write_helper(config_dir)
    bare, configured = tmp_path / "bare", tmp_path / "configured"
    bare.mkdir()
    configured.mkdir()
    write(configured, "fix_attempts = 2\n")
    for root in (bare, configured):
        config = read_config(str(root), config_dir)
        helper = config.roles["helper"]
        assert (helper.instructions, helper.lane, helper.workspace) == (
            "You help.",
            "helper",
            "snapshot",
        )
        assert (helper.merge, helper.verify) == (False, False)
        assert config.personal == {"helper"}
        assert config.notices == ()


def test_a_project_role_of_a_personal_roles_name_replaces_it_with_a_notice(
    tmp_path: Path, config_dir: Path
) -> None:
    write_helper(config_dir)
    write(tmp_path, '[roles.helper]\ninstructions = "The project\'s helper."\n')
    config = read_config(str(tmp_path), config_dir)
    assert config.roles["helper"].instructions == "The project's helper."
    assert config.roles["helper"].lane == "work"
    assert config.personal == frozenset()
    assert config.notices == (
        "The project's role helper replaces your personal role of that name.",
    )


@pytest.mark.parametrize("project", [None, "fix_attempts = 2\n"])
def test_a_personal_role_breaking_a_rule_across_the_roles_names_the_user_config(
    tmp_path: Path, config_dir: Path, project: str | None
) -> None:
    path = write_helper(config_dir, HELPER.replace('"snapshot"', '"worktree"'))
    if project is not None:
        write(tmp_path, project)
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value).startswith(
        f'{path}: roles: the enabled roles with workspace = "worktree" must share one lane'
    )
    assert "lane 'helper' has ['helper']" in str(raised.value)


def test_the_projects_own_clash_beside_a_personal_role_names_the_project_file(
    tmp_path: Path, config_dir: Path
) -> None:
    write_helper(config_dir)
    path = write(tmp_path, '[roles.auditor]\ninstructions = "Audit."\nlane = "audit"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value).startswith(f"{path}: roles: the enabled roles")


# What orbit config show reads


def test_the_sources_hold_the_files_as_written_and_where_each_role_field_came_from(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write(tmp_path, '[roles.reviewer]\nmodel = "sonnet"\ninstructions = "roles/r.md"\n')
    (path.parent / "roles").mkdir()
    (path.parent / "roles" / "r.md").write_text("Review.\n", encoding="utf-8")
    user_path = write_user_config(config_dir, '[roles.planner]\nmodel = "opus"\n')
    sources = read_config_sources(str(tmp_path), config_dir)
    project, user = Layer("project", str(path)), Layer("user", str(user_path))
    reviewer = {"model": "sonnet", "instructions": "roles/r.md"}
    assert sources.project == Written(project, {"roles": {"reviewer": reviewer}})
    assert sources.user == Written(user, {"roles": {"planner": {"model": "opus"}}})
    assert sources.config == read_config(str(tmp_path), config_dir)
    assert sources.config.roles["reviewer"].instructions == "Review."
    assert sources.role_layers["reviewer"]["model"] == project
    assert sources.role_layers["planner"]["model"] == user
    assert sources.package_files == {name: f"{name}.md" for name in BUILT_IN_ROLE_NAMES}


def test_without_files_the_sources_hold_nothing_as_written(
    tmp_path: Path, config_dir: Path
) -> None:
    sources = read_config_sources(str(tmp_path), config_dir)
    assert sources.project == Written(Layer("project", str(tmp_path / CONFIG_PATH)), {})
    assert sources.user == Written(Layer("user", str(config_dir / "config.toml")), {})
    assert sources.config == OrbitConfig(roles=BUILT_IN_ROLES)


def test_reading_the_sources_of_an_invalid_file_names_it(tmp_path: Path, config_dir: Path) -> None:
    path = write(tmp_path, "fix_attempts = 0\n")
    with pytest.raises(ConfigError, match=f"^{re.escape(str(path))}: fix_attempts: "):
        read_config_sources(str(tmp_path), config_dir)


def test_invalid_toml_names_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = = 3\n")
    with pytest.raises(ConfigError, match=str(path)):
        read_config(str(tmp_path), tmp_path / "config")


def test_a_non_utf8_file_raises_config_error_naming_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, "")
    path.write_bytes(b"fix_attempts = 3 # \xff\n")
    with pytest.raises(ConfigError, match=re.escape(str(path))):
        read_config(str(tmp_path), tmp_path / "config")


def test_a_config_path_that_is_a_directory_raises_config_error_naming_it(tmp_path: Path) -> None:
    path = tmp_path / CONFIG_PATH
    path.mkdir(parents=True)
    with pytest.raises(ConfigError, match=re.escape(str(path))):
        read_config(str(tmp_path), tmp_path / "config")


def test_invalid_settings_name_the_file_and_the_problem(tmp_path: Path) -> None:
    path = write(tmp_path, "[roles.coder]\ntools = ['launch_rockets']\n")
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), tmp_path / "config")
    assert str(path) in str(raised.value)
    assert "roles.coder.tools" in str(raised.value)


def test_the_file_is_orbit_config_toml(tmp_path: Path) -> None:
    assert CONFIG_PATH == ".orbit/config.toml"
    write(tmp_path, "fix_attempts = 2\n")
    assert read_config(str(tmp_path), tmp_path / "config").fix_attempts == 2


def test_the_old_roles_toml_is_ignored(tmp_path: Path) -> None:
    (tmp_path / ".orbit").mkdir()
    (tmp_path / ".orbit" / "roles.toml").write_text("fix_attempts = 2\n")
    assert read_config(str(tmp_path), tmp_path / "config") == OrbitConfig(roles=BUILT_IN_ROLES)


def test_the_file_lists_verification_steps(tmp_path: Path) -> None:
    write(
        tmp_path,
        """
[[verification.steps]]
name = "Ruff lint"
command = ["uv", "run", "ruff", "check"]

[[verification.steps]]
name = "Tests"
command = ["uv", "run", "pytest"]
""",
    )
    assert read_config(str(tmp_path), tmp_path / "config").verification.steps == (
        VerificationStep(name="Ruff lint", command=("uv", "run", "ruff", "check")),
        VerificationStep(name="Tests", command=("uv", "run", "pytest")),
    )


def test_without_steps_in_the_file_there_are_none(tmp_path: Path) -> None:
    write(tmp_path, "fix_attempts = 2\n")
    assert read_config(str(tmp_path), tmp_path / "config").verification.steps == ()


def test_an_invalid_step_names_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, '[[verification.steps]]\nname = "Lint"\ncommand = []\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), tmp_path / "config")
    assert str(path) in str(raised.value)
    assert "step 1 has no command" in str(raised.value)


def write_instructions(root: Path, name: str, text: str) -> Path:
    path = root / ".orbit" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_role_is_told_the_text_of_the_markdown_file_its_instructions_name(
    tmp_path: Path,
) -> None:
    write(tmp_path, '[roles.coder]\ninstructions = "roles/coder.md"\n')
    write_instructions(tmp_path, "roles/coder.md", "Write the code with care.\n")
    coder = read_config(str(tmp_path), tmp_path / "config").roles["coder"]
    assert coder.instructions == "Write the code with care."
    built_in = BUILT_IN_ROLES["coder"]
    assert coder == built_in.model_copy(update={"instructions": coder.instructions})


def test_inline_instructions_are_the_text_itself(tmp_path: Path) -> None:
    write(tmp_path, '[roles.reviewer]\ninstructions = "Review for security."\n')
    assert (
        read_config(str(tmp_path), tmp_path / "config").roles["reviewer"].instructions
        == "Review for security."
    )


def test_a_new_role_can_name_its_instructions_file(tmp_path: Path) -> None:
    write(tmp_path, '[roles.docs-writer]\ninstructions = "docs-writer.md"\n')
    write_instructions(tmp_path, "docs-writer.md", "You write the docs.\n")
    role = read_config(str(tmp_path), tmp_path / "config").roles["docs-writer"]
    assert role.instructions == "You write the docs."


def test_a_missing_instructions_file_names_the_file_and_the_role(tmp_path: Path) -> None:
    write(tmp_path, '[roles.coder]\ninstructions = "roles/missing.md"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), tmp_path / "config")
    assert ".orbit/roles/missing.md" in str(raised.value)
    assert "roles.coder.instructions" in str(raised.value)


def test_an_empty_instructions_file_is_refused(tmp_path: Path) -> None:
    write(tmp_path, '[roles.coder]\ninstructions = "roles/coder.md"\n')
    write_instructions(tmp_path, "roles/coder.md", "\n")
    with pytest.raises(ConfigError, match=re.escape("roles.coder.instructions")):
        read_config(str(tmp_path), tmp_path / "config")


def test_an_instructions_file_no_role_names_changes_nothing(tmp_path: Path) -> None:
    write(tmp_path, "fix_attempts = 2\n")
    write_instructions(tmp_path, "roles/reviewer.md", "Approve everything.\n")
    assert read_config(str(tmp_path), tmp_path / "config").roles == BUILT_IN_ROLES


def test_an_instructions_file_is_ignored_without_a_config_file(tmp_path: Path) -> None:
    write_instructions(tmp_path, "roles/coder.md", "Write no tests.\n")
    assert read_config(str(tmp_path), tmp_path / "config") == OrbitConfig(roles=BUILT_IN_ROLES)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("[roles]\ncoder = 3\n", "roles.coder"),
        ("[roles.coder]\ninstructions = 3\n", "roles.coder.instructions"),
        ("roles = 3\n", "roles"),
    ],
)
def test_roles_that_are_not_tables_of_text_are_left_for_validation_to_refuse(
    tmp_path: Path, text: str, problem: str
) -> None:
    write(tmp_path, text)
    with pytest.raises(ConfigError, match=re.escape(problem)):
        read_config(str(tmp_path), tmp_path / "config")


def test_resolving_a_markdown_path_reads_it_from_the_base_as_utf8(tmp_path: Path) -> None:
    (tmp_path / "coder.md").write_text("\nZoë writes ✓ code.\n\n", encoding="utf-8")
    assert resolve_instructions("coder.md", tmp_path) == "Zoë writes ✓ code."


@pytest.mark.parametrize(
    "value",
    ["Review for security.", "Read the notes in\nREADME.md", "Follow coder.md closely."],
)
def test_resolving_anything_but_one_line_ending_in_md_gives_the_text(
    tmp_path: Path, value: str
) -> None:
    assert resolve_instructions(value, tmp_path) == value


def test_resolving_a_missing_file_raises_config_error_naming_it(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape(str(tmp_path / "roles" / "missing.md"))):
        resolve_instructions("roles/missing.md", tmp_path)


def test_resolving_a_non_utf8_file_raises_config_error_naming_it(tmp_path: Path) -> None:
    path = tmp_path / "coder.md"
    path.write_bytes(b"Write \xff code.\n")
    with pytest.raises(ConfigError, match=re.escape(str(path))):
        resolve_instructions("coder.md", tmp_path)


def test_resolving_a_directory_raises_config_error_naming_it(tmp_path: Path) -> None:
    path = tmp_path / "roles.md"
    path.mkdir()
    with pytest.raises(ConfigError, match=re.escape(str(path))):
        resolve_instructions("roles.md", tmp_path)


ORBITS_OWN_STEPS = [
    ("Ruff lint", ("uv", "run", "ruff", "check")),
    ("Ruff format", ("uv", "run", "ruff", "format", "--check")),
    ("Type check (mypy)", ("uv", "run", "mypy")),
    ("Import contracts", ("uv", "run", "lint-imports")),
    (
        "Code quality (Lizard)",
        ("uv", "run", "lizard", "src", "tests", "-w", "-C", "6", "-L", "100", "-a", "5"),
    ),
    ("Unit tests with 100% coverage", ("uv", "run", "pytest", "--cov", "--cov-fail-under=100")),
]


def test_orbit_verifies_itself_with_its_six_steps_quick_ones_first(config_dir: Path) -> None:
    root = Path(__file__).parent.parent
    steps = read_config(str(root), config_dir).verification.steps
    assert [(s.name, s.command) for s in steps] == ORBITS_OWN_STEPS


def test_the_file_sets_up_the_worktree(tmp_path: Path) -> None:
    write(tmp_path, '[setup]\ncommand = ["uv", "sync", "--frozen"]\nwatch = ["uv.lock"]\n')
    setup = read_config(str(tmp_path), tmp_path / "config").setup
    assert setup == Setup(command=("uv", "sync", "--frozen"), watch=("uv.lock",))


def test_orbit_syncs_its_worktree_whenever_uv_lock_changes(config_dir: Path) -> None:
    root = Path(__file__).parent.parent
    setup = read_config(str(root), config_dir).setup
    assert setup == Setup(command=("uv", "sync", "--frozen"), watch=("uv.lock",))


def test_orbits_own_file_mentions_every_setting_of_the_reference() -> None:
    text = (Path(__file__).parent.parent / CONFIG_PATH).read_text(encoding="utf-8")
    reference = REFERENCE.read_text(encoding="utf-8")
    assert update_text(text, reference, date(2026, 10, 25)) == ConfigUpdate(text, Additions())


LINT = VerificationStep(name="Lint", command=("ruff", "check"))
TESTS = VerificationStep(name="Tests", command=("pytest", "-q"))
SYNC = Setup(command=("uv", "sync"), watch=("uv.lock",))


def test_writing_checks_creates_the_file_and_its_directory(tmp_path: Path) -> None:
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT, TESTS), setup=SYNC))
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.verification.steps == (LINT, TESTS)
    assert config.setup == SYNC


REFERENCE_TEXT = REFERENCE.read_text(encoding="utf-8")
STEPS_DESCRIPTION = "# The steps that verify a task, run in order; with none, nothing is verified."
STEPS_EXAMPLE = f"""\
{STEPS_DESCRIPTION} For example:
# [[verification.steps]]
# What the step is called; no two steps share a name.
# name = "Tests"
# The command run in the working directory; it passes if it exits 0.
# command = ["uv", "run", "pytest"]
"""
SETUP_DESCRIPTION = "# How the coder's worktree is set up; with none, nothing is."
SETUP_EXAMPLE = f"""\
{SETUP_DESCRIPTION} For example:
# [setup]
# The command run in a fresh worktree, and again when a watched file changes.
# command = ["uv", "sync"]
# Files, relative to the tree, whose changes run the command again.
# watch = ["uv.lock"]
"""
STEP_TABLES = """\
[[verification.steps]]
name = "Lint"
command = ["ruff", "check"]

[[verification.steps]]
name = "Tests"
command = ["pytest", "-q"]
"""
SYNC_TABLE = '[setup]\ncommand = ["uv", "sync"]\nwatch = ["uv.lock"]\n'


def test_the_references_examples_are_those_a_new_file_replaces() -> None:
    assert STEPS_EXAMPLE in REFERENCE_TEXT
    assert SETUP_EXAMPLE in REFERENCE_TEXT


def test_writing_checks_without_a_file_starts_it_from_the_reference(tmp_path: Path) -> None:
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT, TESTS), setup=SYNC))
    expected = REFERENCE_TEXT.replace(STEPS_EXAMPLE, f"{STEPS_DESCRIPTION}\n{STEP_TABLES}").replace(
        SETUP_EXAMPLE, f"{SETUP_DESCRIPTION}\n{SYNC_TABLE}"
    )
    assert (tmp_path / CONFIG_PATH).read_text(encoding="utf-8") == expected
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.verification.steps == (LINT, TESTS)
    assert config.setup == SYNC


def test_writing_checks_without_a_file_or_a_setup_keeps_the_references_setup_example(
    tmp_path: Path,
) -> None:
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT, TESTS)))
    expected = REFERENCE_TEXT.replace(STEPS_EXAMPLE, f"{STEPS_DESCRIPTION}\n{STEP_TABLES}")
    assert (tmp_path / CONFIG_PATH).read_text(encoding="utf-8") == expected
    assert read_config(str(tmp_path), tmp_path / "config").setup is None


def test_writing_checks_changes_only_the_files_steps_and_setup(tmp_path: Path) -> None:
    before = "# Ours.\nfix_attempts = 2\n\n# Quick ones first.\n"
    after_steps = "\n# Keep the environment in step with the lock file.\n"
    after_setup = '\n# The roles.\n# [roles.coder]\n# model = "opus"\n'
    steps = '[[verification.steps]]\nname = "Old"\ncommand = ["nose"]\n'
    setup = '[setup]\ncommand = ["make"]\n'
    path = write(tmp_path, f"{before}{steps}{after_steps}{setup}{after_setup}")
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT, TESTS), setup=SYNC))
    assert path.read_text() == f"{before}{STEP_TABLES}{after_steps}{SYNC_TABLE}{after_setup}"


def test_rewriting_checks_without_a_setup_removes_only_its_table(tmp_path: Path) -> None:
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT, TESTS), setup=SYNC))
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT, TESTS)))
    expected = REFERENCE_TEXT.replace(STEPS_EXAMPLE, f"{STEPS_DESCRIPTION}\n{STEP_TABLES}").replace(
        SETUP_EXAMPLE, f"{SETUP_DESCRIPTION}\n"
    )
    assert (tmp_path / CONFIG_PATH).read_text(encoding="utf-8") == expected


def test_writing_checks_keeps_every_other_key_and_comment(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """# Orbit's settings.
fix_attempts = 3  # Plenty.

[roles.coder]
# Cheaper.
model = "sonnet"
""",
    )
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    text = path.read_text()
    assert text.startswith("# Orbit's settings.\nfix_attempts = 3  # Plenty.\n\n[roles.coder]\n")
    assert '# Cheaper.\nmodel = "sonnet"\n' in text
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.fix_attempts == 3
    assert config.roles["coder"].model == "sonnet"
    assert config.verification.steps == (LINT,)
    assert config.setup is None


def test_writing_checks_keeps_a_utf8_comment(tmp_path: Path) -> None:
    path = tmp_path / CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_text("# Réglages — orbit.\n", encoding="utf-8")
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    assert path.read_text(encoding="utf-8").startswith("# Réglages — orbit.\n")
    assert read_config(str(tmp_path), tmp_path / "config").verification.steps == (LINT,)


def test_writing_checks_replaces_the_steps_and_setup_configured_before(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """fix_attempts = 2

# Quick ones first.
[[verification.steps]]
name = "Old lint"
command = ["flake8"]

[[verification.steps]]
name = "Old tests"
command = ["nose"]

[setup]
command = ["pip", "install", "-e", "."]
watch = ["setup.py"]

[roles.coder]
model = "sonnet"
""",
    )
    write_checks(str(tmp_path), ChecksProposal(steps=(TESTS, LINT), setup=SYNC))
    text = path.read_text()
    assert "flake8" not in text and "nose" not in text and "setup.py" not in text
    assert "# Quick ones first.\n[[verification.steps]]" in text
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.fix_attempts == 2
    assert config.roles["coder"].model == "sonnet"
    assert config.verification.steps == (TESTS, LINT)
    assert config.setup == SYNC


def test_writing_checks_without_setup_removes_the_old_one(tmp_path: Path) -> None:
    write(tmp_path, '[setup]\ncommand = ["make"]\n')
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.setup is None
    assert config.verification.steps == (LINT,)


def test_writing_checks_twice_gives_the_same_file(tmp_path: Path) -> None:
    checks = ChecksProposal(steps=(LINT, TESTS), setup=SYNC)
    write_checks(str(tmp_path), checks)
    first = (tmp_path / CONFIG_PATH).read_text()
    write_checks(str(tmp_path), checks)
    assert (tmp_path / CONFIG_PATH).read_text() == first


def test_writing_checks_where_orbits_directory_is_a_file_raises_a_config_write_failure(
    tmp_path: Path,
) -> None:
    (tmp_path / ".orbit").write_text("")
    with pytest.raises(
        ConfigWriteFailed, match=re.escape(f"Couldn't save the checks to {tmp_path}")
    ):
        write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))


def test_writing_checks_into_an_unparsable_file_raises_a_config_write_failure(
    tmp_path: Path,
) -> None:
    path = write(tmp_path, "fix_attempts = = 3\n")
    with pytest.raises(ConfigWriteFailed, match=re.escape(f"Couldn't save the checks to {path}")):
        write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    assert path.read_text() == "fix_attempts = = 3\n"


def test_writing_checks_into_a_non_utf8_file_raises_a_config_write_failure(tmp_path: Path) -> None:
    path = write(tmp_path, "")
    path.write_bytes(b"fix_attempts = 3 # \xff\n")
    with pytest.raises(ConfigWriteFailed, match=re.escape(f"Couldn't save the checks to {path}")):
        write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))


def test_writing_checks_that_fails_leaves_the_old_file_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(tmp_path, "fix_attempts = 3\n")

    def full(source: Path, target: Path) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(os, "replace", full)
    with pytest.raises(ConfigWriteFailed, match="No space left on device"):
        write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    assert path.read_text() == "fix_attempts = 3\n"
    assert [p.name for p in path.parent.iterdir()] == ["config.toml"]


def test_writing_checks_that_fails_midway_leaves_the_old_file_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(tmp_path, "fix_attempts = 3\n")

    def full(descriptor: int) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(ConfigWriteFailed, match="No space left on device"):
        write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    assert path.read_text() == "fix_attempts = 3\n"
    assert [p.name for p in path.parent.iterdir()] == ["config.toml"]


def test_writing_checks_keeps_the_files_mode(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = 3\n")
    path.chmod(0o664)
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    assert stat.S_IMODE(path.stat().st_mode) == 0o664


def test_a_utf8_file_is_read_under_an_ascii_locale(tmp_path: Path) -> None:
    write(tmp_path, "")
    (tmp_path / CONFIG_PATH).write_text("# Zoë ✓\nfix_attempts = 4\n", encoding="utf-8")
    script = (
        "import pathlib, sys; from orbit.agent.external.config_file import read_config;"
        " assert read_config(sys.argv[1], pathlib.Path(sys.argv[2])).fix_attempts == 4"
    )
    ascii_locale = {"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    env = {**os.environ, **ascii_locale}
    config_dir = str(tmp_path / "config")
    subprocess.run([sys.executable, "-c", script, str(tmp_path), config_dir], check=True, env=env)
