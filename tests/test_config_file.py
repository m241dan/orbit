import re
from pathlib import Path

import pytest

from orbit.agent.domain.roles import ChecksProposal, ConfigError, OrbitConfig, Setup
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.agent.external.config_file import CONFIG_PATH, read_config, write_checks

BUILT_IN_ROLES = read_built_in_roles()


def write(root: Path, text: str) -> Path:
    path = root / CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_without_a_file_the_defaults_apply(tmp_path: Path) -> None:
    assert read_config(str(tmp_path)) == OrbitConfig(roles=BUILT_IN_ROLES)


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
    config = read_config(str(tmp_path))
    assert config.fix_attempts == 3
    assert config.roles["coder"].model == "sonnet"
    assert config.roles["reviewer"] == BUILT_IN_ROLES["reviewer"]


def test_invalid_toml_names_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = = 3\n")
    with pytest.raises(ConfigError, match=str(path)):
        read_config(str(tmp_path))


def test_a_non_utf8_file_raises_config_error_naming_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, "")
    path.write_bytes(b"fix_attempts = 3 # \xff\n")
    with pytest.raises(ConfigError, match=re.escape(str(path))):
        read_config(str(tmp_path))


def test_a_config_path_that_is_a_directory_raises_config_error_naming_it(tmp_path: Path) -> None:
    path = tmp_path / CONFIG_PATH
    path.mkdir(parents=True)
    with pytest.raises(ConfigError, match=re.escape(str(path))):
        read_config(str(tmp_path))


def test_invalid_settings_name_the_file_and_the_problem(tmp_path: Path) -> None:
    path = write(tmp_path, "[roles.coder]\ntools = ['launch_rockets']\n")
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path))
    assert str(path) in str(raised.value)
    assert "roles.coder.tools" in str(raised.value)


def test_the_file_is_orbit_config_toml(tmp_path: Path) -> None:
    assert CONFIG_PATH == ".orbit/config.toml"
    write(tmp_path, "fix_attempts = 2\n")
    assert read_config(str(tmp_path)).fix_attempts == 2


def test_the_old_roles_toml_is_ignored(tmp_path: Path) -> None:
    (tmp_path / ".orbit").mkdir()
    (tmp_path / ".orbit" / "roles.toml").write_text("fix_attempts = 2\n")
    assert read_config(str(tmp_path)) == OrbitConfig(roles=BUILT_IN_ROLES)


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
    assert read_config(str(tmp_path)).verification.steps == (
        VerificationStep(name="Ruff lint", command=("uv", "run", "ruff", "check")),
        VerificationStep(name="Tests", command=("uv", "run", "pytest")),
    )


def test_without_steps_in_the_file_there_are_none(tmp_path: Path) -> None:
    write(tmp_path, "fix_attempts = 2\n")
    assert read_config(str(tmp_path)).verification.steps == ()


def test_an_invalid_step_names_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, '[[verification.steps]]\nname = "Lint"\ncommand = []\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path))
    assert str(path) in str(raised.value)
    assert "step 1 has no command" in str(raised.value)


ORBITS_OWN_STEPS = [
    ("Ruff lint", ("uv", "run", "ruff", "check")),
    ("Ruff format", ("uv", "run", "ruff", "format", "--check")),
    ("Type check (mypy)", ("uv", "run", "mypy")),
    ("Import contracts", ("uv", "run", "lint-imports")),
    (
        "Code quality (Lizard)",
        ("uv", "run", "lizard", "src", "tests", "-w", "-C", "6", "-L", "100", "-a", "5"),
    ),
    ("Unit tests, 100% coverage", ("uv", "run", "pytest", "--cov", "--cov-fail-under=100")),
]


def test_orbit_verifies_itself_with_its_six_steps_quick_ones_first() -> None:
    root = Path(__file__).parent.parent
    steps = read_config(str(root)).verification.steps
    assert [(s.name, s.command) for s in steps] == ORBITS_OWN_STEPS


def test_the_file_sets_up_the_worktree(tmp_path: Path) -> None:
    write(tmp_path, '[setup]\ncommand = ["uv", "sync", "--frozen"]\nwatch = ["uv.lock"]\n')
    setup = read_config(str(tmp_path)).setup
    assert setup == Setup(command=("uv", "sync", "--frozen"), watch=("uv.lock",))


def test_orbit_syncs_its_worktree_whenever_uv_lock_changes() -> None:
    root = Path(__file__).parent.parent
    setup = read_config(str(root)).setup
    assert setup == Setup(command=("uv", "sync", "--frozen"), watch=("uv.lock",))


LINT = VerificationStep(name="Lint", command=("ruff", "check"))
TESTS = VerificationStep(name="Tests", command=("pytest", "-q"))
SYNC = Setup(command=("uv", "sync"), watch=("uv.lock",))


def test_writing_checks_creates_the_file_and_its_directory(tmp_path: Path) -> None:
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT, TESTS), setup=SYNC))
    config = read_config(str(tmp_path))
    assert config.verification.steps == (LINT, TESTS)
    assert config.setup == SYNC


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
    config = read_config(str(tmp_path))
    assert config.fix_attempts == 3
    assert config.roles["coder"].model == "sonnet"
    assert config.verification.steps == (LINT,)
    assert config.setup is None


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
    config = read_config(str(tmp_path))
    assert config.fix_attempts == 2
    assert config.roles["coder"].model == "sonnet"
    assert config.verification.steps == (TESTS, LINT)
    assert config.setup == SYNC


def test_writing_checks_without_setup_removes_the_old_one(tmp_path: Path) -> None:
    write(tmp_path, '[setup]\ncommand = ["make"]\n')
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    config = read_config(str(tmp_path))
    assert config.setup is None
    assert config.verification.steps == (LINT,)


def test_writing_checks_twice_gives_the_same_file(tmp_path: Path) -> None:
    checks = ChecksProposal(steps=(LINT, TESTS), setup=SYNC)
    write_checks(str(tmp_path), checks)
    first = (tmp_path / CONFIG_PATH).read_text()
    write_checks(str(tmp_path), checks)
    assert (tmp_path / CONFIG_PATH).read_text() == first


def test_writing_checks_where_orbits_directory_is_a_file_raises_runtime_error(
    tmp_path: Path,
) -> None:
    (tmp_path / ".orbit").write_text("")
    with pytest.raises(RuntimeError, match=re.escape(f"Couldn't save the checks to {tmp_path}")):
        write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))


def test_writing_checks_into_an_unparsable_file_raises_runtime_error(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = = 3\n")
    with pytest.raises(RuntimeError, match=re.escape(f"Couldn't save the checks to {path}")):
        write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    assert path.read_text() == "fix_attempts = = 3\n"


def test_writing_checks_into_a_non_utf8_file_raises_runtime_error(tmp_path: Path) -> None:
    path = write(tmp_path, "")
    path.write_bytes(b"fix_attempts = 3 # \xff\n")
    with pytest.raises(RuntimeError, match=re.escape(f"Couldn't save the checks to {path}")):
        write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
