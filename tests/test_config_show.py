"""``orbit config show``: the configuration resolved for a workspace, each value with its layer,
as text or JSON."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from orbit.agent import INVALID_CONFIG
from orbit.agent.domain.config_report import Instructions, Setting
from orbit.agent.domain.layers import DEFAULT, Layer
from orbit.agent.domain.roles import Role
from orbit.agent.external.built_in_roles import ROLE_DIRECTORY
from orbit.cli import app
from orbit.cli.config_text import config_json, config_text
from orbit.protocol import BUILT_IN_ROLE_NAMES

runner = CliRunner()

REVIEWER = (ROLE_DIRECTORY / "reviewer.md").read_text(encoding="utf-8").strip()
"""The text of the built-in reviewer's instructions."""

REVIEWER_LINES = len(REVIEWER.splitlines())


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A home directory of its own, apart from the config directory every test has."""
    path = tmp_path / "home"
    monkeypatch.setenv("HOME", str(path))
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A workspace outside git, with no project file yet."""
    root = tmp_path / "project"
    root.mkdir()
    return root


def project_file(root: Path, text: str) -> Path:
    """The project file of the workspace at ``root``, holding ``text``."""
    path = root / ".orbit" / "config.toml"
    path.parent.mkdir()
    path.write_text(text)
    return path


def user_file(config_dir: Path, text: str) -> Path:
    """The user config in ``config_dir``, holding ``text``."""
    path = config_dir / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def show(project: Path, *args: str) -> Result:
    """``orbit config show`` run on the workspace at ``project`` with ``args``."""
    return runner.invoke(app, ["config", "show", "-C", str(project), *args])


def test_roles_lists_each_role_field_with_the_layer_it_came_from(
    project: Path, config_dir: Path
) -> None:
    project_file(project, '[roles.reviewer]\nmodel = "sonnet"\n')
    user_path = user_file(config_dir, '[roles.planner]\nmodel = "opus"\n')
    result = show(project, "roles")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert 'roles.reviewer.model = "sonnet"   project .orbit/config.toml' in lines
    assert f'roles.planner.model = "opus"   user {user_path}' in lines
    tools = '["get_task", "complete_task", "run_check", "report_progress", "give_up"]'
    assert f"roles.coder.tools = {tools}   package" in lines
    assert "roles.coder.effort = null   default" in lines
    assert all(line.startswith("roles.") for line in lines if line)


def test_a_user_config_under_home_shows_from_there(project: Path, home: Path) -> None:
    config_dir = home / ".config" / "orbit"
    user_file(config_dir, '[roles.planner]\nmodel = "opus"\n')
    result = show(project, "roles.planner.model", "--config-dir", str(config_dir))
    assert result.stdout == 'roles.planner.model = "opus"   user ~/.config/orbit/config.toml\n'


def test_a_key_of_one_value_prints_its_line(project: Path) -> None:
    result = show(project, "server.port")
    assert result.exit_code == 0, result.output
    assert result.stdout == "server.port = 7433   default\n"


def test_a_roles_runtime_shows_with_its_layer(project: Path) -> None:
    result = show(project, "roles.coder")
    assert result.exit_code == 0, result.output
    assert 'roles.coder.runtime = "claude"   package' in result.stdout.splitlines()


def test_the_user_configs_runtime_for_every_role_is_the_users(
    project: Path, config_dir: Path
) -> None:
    path = user_file(config_dir, '[roles]\nruntime = "claude"\n')
    result = show(project, "roles.reviewer.runtime")
    assert result.stdout == f'roles.reviewer.runtime = "claude"   user {path}\n'


def test_a_value_the_user_config_sets_is_the_users(project: Path, config_dir: Path) -> None:
    path = user_file(config_dir, "[telemetry]\nexporter = 'file'\n")
    result = show(project, "telemetry.exporter")
    assert result.stdout == f'telemetry.exporter = "file"   user {path}\n'


def test_without_a_key_every_section_is_listed_in_groups_in_order(project: Path) -> None:
    result = show(project)
    assert result.exit_code == 0, result.output
    groups = result.stdout.rstrip("\n").split("\n\n")
    assert [group.split(" = ")[0] for group in groups] == [
        "fix_attempts",
        *(f"roles.{name}.instructions" for name in BUILT_IN_ROLE_NAMES),
        "verification.steps",
        "setup",
        "storage.tool_output_kb",
        "server.tls",
        "telemetry.exporter",
    ]
    assert "max_checks = 20   default" in groups[0].splitlines()


def test_the_projects_steps_and_setup_are_listed_from_its_file(project: Path) -> None:
    project_file(
        project,
        '[[verification.steps]]\nname = "Lint"\ncommand = ["ruff", "check"]\n\n'
        '[setup]\ncommand = ["uv", "sync"]\n',
    )
    result = show(project, "verification")
    assert result.stdout == (
        'verification.steps.1.name = "Lint"   project .orbit/config.toml\n'
        'verification.steps.1.command = ["ruff", "check"]   project .orbit/config.toml\n'
    )
    assert show(project, "setup.watch").stdout == "setup.watch = []   default\n"


def test_instructions_show_their_file_and_length(project: Path) -> None:
    result = show(project, "roles.reviewer.instructions")
    assert result.stdout == (
        f"roles.reviewer.instructions = (reviewer.md, {REVIEWER_LINES} lines)   package\n"
    )


def test_full_prints_the_instructions_text(project: Path) -> None:
    result = show(project, "roles.reviewer.instructions", "--full")
    assert result.stdout == f'roles.reviewer.instructions = """\n{REVIEWER}\n"""   package\n'


def test_instructions_given_as_text_show_as_inline(project: Path) -> None:
    project_file(project, '[roles.coder]\ninstructions = "Code.\\nTest."\n')
    result = show(project, "roles.coder.instructions")
    assert result.stdout == (
        "roles.coder.instructions = (inline, 2 lines)   project .orbit/config.toml\n"
    )


def test_json_gives_the_same_values_and_layers(project: Path, config_dir: Path) -> None:
    path = project_file(project, '[roles.reviewer]\nmodel = "sonnet"\n')
    user_path = user_file(config_dir, '[roles.planner]\nmodel = "opus"\n')
    result = show(project, "roles", "--json")
    assert result.exit_code == 0, result.output
    roles = json.loads(result.stdout)["roles"]
    assert roles["reviewer"]["model"] == {"value": "sonnet", "layer": "project", "file": str(path)}
    assert roles["planner"]["model"] == {"value": "opus", "layer": "user", "file": str(user_path)}
    assert roles["coder"]["tools"]["layer"] == "package"
    assert roles["coder"]["tools"]["file"] is None
    summary = f"(reviewer.md, {REVIEWER_LINES} lines)"
    assert roles["reviewer"]["instructions"]["value"] == summary


def test_json_with_full_gives_the_instructions_text(project: Path) -> None:
    result = show(project, "roles.reviewer.instructions", "--json", "--full")
    instructions = json.loads(result.stdout)["roles"]["reviewer"]["instructions"]
    assert instructions == {"value": REVIEWER, "layer": "package", "file": None}


def test_json_holds_only_the_keys_subtree(project: Path) -> None:
    result = show(project, "server.port", "--json")
    assert json.loads(result.stdout) == {
        "server": {"port": {"value": 7433, "layer": "default", "file": None}}
    }


def test_an_unknown_key_exits_2_naming_it(project: Path) -> None:
    result = show(project, "roles.nope")
    assert result.exit_code == 2
    assert "roles.nope" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("project_text", "user_text", "problem"),
    [
        ("fix_attempts = 0\n", None, "project"),
        (None, "[roles.coder]\nverify = false\n", "user"),
        (None, "[server]\nport = -1\n", "user"),
        (None, "[telemetry]\nexporter = 'nowhere'\n", "user"),
    ],
)
def test_an_invalid_configuration_exits_2_naming_its_file(
    project: Path, config_dir: Path, project_text: str | None, user_text: str | None, problem: str
) -> None:
    paths = {
        "project": project_file(project, project_text) if project_text else None,
        "user": user_file(config_dir, user_text) if user_text else None,
    }
    result = show(project)
    assert result.exit_code == 2
    assert result.stderr.startswith(f"{INVALID_CONFIG}{paths[problem]}: ")
    assert result.stdout == ""


ROLE_NAME_PROBLEM = "roles.qa.lead: a role's name is a plain name: letters, digits, - and _"

QA_LEAD = """
[roles."qa.lead"]
instructions = "Lead."
lane = "qa"
workspace = "snapshot"
merge = false
verify = false
"""
"""A role whose quoted name has a dot in it."""


@pytest.mark.parametrize("problem", ["project", "user"])
def test_a_role_whose_name_isnt_plain_exits_2_naming_its_key_and_file(
    project: Path, config_dir: Path, problem: str
) -> None:
    if problem == "project":
        path = project_file(project, QA_LEAD)
    else:
        path = user_file(config_dir, QA_LEAD)
    result = show(project)
    assert result.exit_code == 2
    assert result.stderr == f"{INVALID_CONFIG}{path}: {ROLE_NAME_PROBLEM}\n"
    assert result.stdout == ""


def test_a_plainly_named_roles_key_lists_only_its_fields(project: Path) -> None:
    project_file(
        project,
        '[roles.qa]\ninstructions = "QA."\n\n[roles.qa-lead]\ninstructions = "Lead."\n\n'
        '[roles.qa_lead]\ninstructions = "Lead too."\n',
    )
    result = show(project, "roles.qa-lead")
    assert result.exit_code == 0, result.output
    keys = [line.split(" = ")[0] for line in result.stdout.splitlines()]
    assert keys == [f"roles.qa-lead.{key}" for key in Role.model_fields]


def test_a_missing_workspace_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["config", "show", "-C", str(tmp_path / "nowhere")])
    assert result.exit_code == 2
    assert "doesn't exist" in result.stderr


def test_showing_writes_nothing(project: Path, tmp_path: Path) -> None:
    project_file(project, "fix_attempts = 2\n")
    before = sorted(tmp_path.rglob("*"))
    assert show(project).exit_code == 0
    assert sorted(tmp_path.rglob("*")) == before


# How it prints


def test_a_file_outside_the_workspace_and_home_shows_its_whole_path() -> None:
    report = {"server.port": Setting(9000, Layer("user", "/etc/orbit/config.toml"))}
    text = config_text(report, full=False, root=Path("/src/app"), home=Path("/home/me"))
    assert text == "server.port = 9000   user /etc/orbit/config.toml"


def test_text_keeps_unicode_as_it_is() -> None:
    report = {"roles.x.model": Setting("zoë", DEFAULT)}
    assert config_text(report, full=False, root=Path("/"), home=Path("/home")) == (
        'roles.x.model = "zoë"   default'
    )


def test_json_of_inline_instructions_says_so() -> None:
    report = {"roles.x.instructions": Setting(Instructions("Do it.", None), DEFAULT)}
    instructions = json.loads(config_json(report, full=False))["roles"]["x"]["instructions"]
    assert instructions["value"] == "(inline, 1 line)"
