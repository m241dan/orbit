"""``orbit config show``: the configuration resolved for a workspace, each value with its layer,
as text or JSON."""

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from orbit.agent import INVALID_CONFIG
from orbit.agent.domain.config_report import Instructions, Setting, SkillFileText, SkillMap
from orbit.agent.domain.layers import DEFAULT, PACKAGE, Layer
from orbit.agent.domain.roles import ROLE_KEYS
from orbit.agent.external.built_in_roles import ROLE_DIRECTORY
from orbit.agent.external.built_in_triggers import TRIGGER_DIRECTORY
from orbit.agent.external.skill_files import SKILL_DIRECTORY
from orbit.cli import app
from orbit.cli.config_text import config_json, config_text, skill_map_json, skill_map_text
from orbit.protocol import BUILT_IN_ROLE_NAMES, LENS_REVIEWERS, SUPER_LENS_REVIEWERS

runner = CliRunner()

REVIEWER = (ROLE_DIRECTORY / "reviewer.md").read_text(encoding="utf-8").strip()
"""The text of the built-in reviewer's instructions."""

REVIEWER_LINES = len(REVIEWER.splitlines())

LENS_TEMPLATE_LINES = len(
    (TRIGGER_DIRECTORY / "lens-review.md").read_text(encoding="utf-8").strip().splitlines()
)
"""How many lines the shipped lens review triggers' template has."""

STARTUP_FIX_TEMPLATE = (TRIGGER_DIRECTORY / "startup-fix.md").read_text(encoding="utf-8").strip()
"""The text of the shipped startup-fix trigger's template."""

SHIPPED = Path(str(SKILL_DIRECTORY))
"""The folder of the skills shipped with orbit, a folder each."""

SKILL_NAMES = sorted(folder.name for folder in SHIPPED.iterdir() if folder.is_dir())
"""The names of the skills shipped with orbit, in order."""

TRIGGER_NAMES = (
    "fit-review",
    "bugs-review",
    "tests-review",
    "logic-review",
    "security-review",
    "performance-review",
    "merge-pass",
    "super-merge-pass",
    "super-review",
    "super-fix-review",
    "configure-checks",
    "set-up-verification",
    "startup-check",
    "startup-fix",
)
"""The names of the triggers shipped with orbit, in order."""

TESTS = "".join(f"Line {number}.\n" for number in range(1, 43))
"""A skill file of 42 lines."""

MAPPINGS = """
[[skills]]
paths = ["src", "noxfile.py"]
use = ["python"]

[[skills]]
paths = ["native"]
use = ["cpp"]
"""
"""Two ``[[skills]]`` entries, for paths :func:`mapped_paths` makes."""

PYTHON_MAPPING = '[[skills]]\npaths = ["src"]\nuse = ["python", "application"]\n'
"""One ``[[skills]]`` entry, for the ``src`` folder :func:`mapped_paths` makes."""

LEGEND = """role: in the role's skills.
task: when the task's list names it.
reviewed task: when the reviewed task's list names it.
mapped: in [[skills]]; given when the task has no list, and always to agents without tasks.
—: never given."""
"""The lines under the skill map, a line per value of its cells."""

SKILLS_ALONE = "--skills shows the whole map; it takes no key or --full.\n"
"""What ``orbit config show --skills`` says, given a key or ``--full``."""


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


def shipped_files(name: str) -> dict[str, str]:
    """The text of each file of the skill ``name`` orbit ships, by its path in the folder, in
    order."""
    folder = SHIPPED / name
    paths = (path for path in folder.rglob("*") if path.is_file() and not path.name.startswith("."))
    texts = {
        path.relative_to(folder).as_posix(): path.read_text(encoding="utf-8") for path in paths
    }
    return dict(sorted(texts.items()))


def counted(text: str) -> str:
    """How many lines ``text`` has, as ``orbit config show`` gives a skill file's value."""
    lines = len(text.splitlines())
    return f"({lines} line{'' if lines == 1 else 's'})"


def listed(name: str) -> str:
    """The lines ``orbit config show`` lists the files of the shipped skill ``name`` in."""
    return "\n".join(
        f"skill_files.{name}.{path} = {counted(text)}   package"
        for path, text in shipped_files(name).items()
    )


def project_skill(root: Path, path: str, text: str) -> Path:
    """The project's skill file at ``path`` in its ``.orbit/skills/`` in the workspace at
    ``root``, holding ``text``."""
    file = root / ".orbit" / "skills" / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    return file


def mapped_paths(root: Path) -> None:
    """The paths :data:`MAPPINGS` maps, made in the workspace at ``root``."""
    (root / "src").mkdir()
    (root / "native").mkdir()
    (root / "noxfile.py").write_text("")


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
    assert f"roles.coder.tools.orbit = {tools}   package" in lines
    assert 'roles.coder.tools.workspace = "shell"   package' in lines
    assert "roles.coder.tools.web = true   package" in lines
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


def test_a_roles_skills_show_in_the_package_layer(project: Path) -> None:
    result = show(project, "roles.reviewer.skills")
    assert result.exit_code == 0, result.output
    skills = (
        '["code-style", "security", "review-fit", "review-bugs", "review-tests", "review-logic",'
        ' "review-security", "review-performance", "review-style", "review-drift",'
        ' "review-scaling"]'
    )
    assert result.stdout == f"roles.reviewer.skills = {skills}   package\n"


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
        *(f"triggers.{name}.on" for name in TRIGGER_NAMES),
        "verification.steps",
        "setup",
        "skills",
        *(f"skill_files.{name}.SKILL.md" for name in SKILL_NAMES),
        "storage.tool_output_kb",
        "server.tls",
        "telemetry.exporter",
    ]
    assert "max_checks = 20   default" in groups[0].splitlines()


def test_a_shipped_triggers_key_lists_its_fields_in_the_package_layer(project: Path) -> None:
    result = show(project, "triggers.bugs-review")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        'triggers.bugs-review.on = "task_landed"   package',
        'triggers.bugs-review.role = "bugs-reviewer"   package',
        "triggers.bugs-review.checks = null   default",
        f"triggers.bugs-review.prompt = (lens-review.md, {LENS_TEMPLATE_LINES} lines)   package",
        "triggers.bugs-review.when = {}   default",
        'triggers.bugs-review.guards = ["review-rounds"]   package',
        "triggers.bugs-review.every = null   default",
        "triggers.bugs-review.manual = false   default",
        "triggers.bugs-review.label = null   default",
        "triggers.bugs-review.draft = false   default",
        "triggers.bugs-review.enabled = true   default",
        'triggers.bugs-review.tab = "review"   package',
        "triggers.bugs-review.paths = null   default",
        "triggers.bugs-review.cron = null   default",
    ]


def test_the_merge_pass_trigger_shows_its_event_role_and_findings(project: Path) -> None:
    result = show(project, "triggers.merge-pass")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert 'triggers.merge-pass.on = "reviews_finished"   package' in lines
    assert 'triggers.merge-pass.role = "reviewer"   package' in lines
    assert (
        'triggers.merge-pass.when = {"findings": "some", "review": ["commit", "batch", "set"]}'
        "   package"
    ) in lines


def test_the_review_trigger_is_no_longer_shipped(project: Path) -> None:
    result = show(project, "triggers")
    assert result.exit_code == 0, result.output
    assert not any(line.startswith("triggers.review.") for line in result.stdout.splitlines())


def test_a_lens_reviewer_shows_its_skills_and_blocking_findings(project: Path) -> None:
    result = show(project, "roles.security-reviewer")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    skills = '["code-style", "security", "review-security"]'
    assert f"roles.security-reviewer.skills = {skills}   package" in lines
    assert "roles.security-reviewer.blocking_findings = true   package" in lines


def test_a_trigger_the_project_overrides_shows_its_fields_from_the_file(project: Path) -> None:
    project_file(project, "[triggers.super-review]\nevery = 5\n")
    result = show(project, "triggers.super-review")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert "triggers.super-review.every = 5   project .orbit/config.toml" in lines
    assert 'triggers.super-review.when = {"origin": ["manual", "planner"]}   package' in lines


def test_a_projects_paths_for_the_security_review_show_in_its_layer(project: Path) -> None:
    project_file(project, '[triggers.security-review]\npaths = ["src/**/external/**"]\n')
    result = show(project, "triggers.security-review.paths")
    assert result.exit_code == 0, result.output
    assert result.stdout == (
        'triggers.security-review.paths = ["src/**/external/**"]   project .orbit/config.toml\n'
    )


def test_a_projects_schedule_trigger_shows_its_cron_in_its_layer(project: Path) -> None:
    nightly = 'on = "schedule"\ncron = "0 3 * * *"\nrole = "coder"\nprompt = "Tidy up"\n'
    project_file(project, f"[triggers.nightly]\n{nightly}")
    result = show(project, "triggers.nightly.cron")
    assert result.exit_code == 0, result.output
    assert result.stdout == 'triggers.nightly.cron = "0 3 * * *"   project .orbit/config.toml\n'


def test_a_trigger_listing_roles_shows_them_as_a_list(project: Path) -> None:
    result = show(project, "triggers.super-review.role")
    assert result.exit_code == 0, result.output
    assert result.stdout == (
        'triggers.super-review.role = ["style-reviewer", "drift-reviewer", "scaling-reviewer"]'
        "   package\n"
    )


def test_full_prints_a_triggers_prompt_text(project: Path) -> None:
    result = show(project, "triggers.startup-fix.prompt", "--full")
    assert result.stdout == (
        f'triggers.startup-fix.prompt = """\n{STARTUP_FIX_TEMPLATE}\n"""   package\n'
    )


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
        "verification.steps.1.set = null   project .orbit/config.toml\n"
        "verification.sets = {}   default\n"
    )
    assert show(project, "setup.watch").stdout == "setup.watch = []   default\n"


def test_the_projects_sets_are_listed_with_its_steps(project: Path) -> None:
    project_file(
        project,
        '[[verification.steps]]\nname = "Stress"\ncommand = ["stress"]\nset = "stress"\n\n'
        "[verification.sets.stress]\nholds_merges = true\n\n"
        '[triggers.nightly]\non = "commits"\nchecks = "stress"\n',
    )
    result = show(project, "verification")
    assert result.stdout == (
        'verification.steps.1.name = "Stress"   project .orbit/config.toml\n'
        'verification.steps.1.command = ["stress"]   project .orbit/config.toml\n'
        'verification.steps.1.set = "stress"   project .orbit/config.toml\n'
        "verification.sets.stress.holds_merges = true   project .orbit/config.toml\n"
    )


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


def test_without_mappings_or_project_skills_there_are_none_and_every_shipped_file(
    project: Path,
) -> None:
    assert show(project, "skills").stdout == "skills = []   default\n"
    result = show(project, "skill_files")
    assert result.exit_code == 0, result.output
    assert result.stdout == "\n\n".join(map(listed, SKILL_NAMES)) + "\n"


def test_each_skills_files_are_a_group_of_their_own(project: Path) -> None:
    groups = show(project, "skill_files").stdout.rstrip("\n").split("\n\n")
    skills = [{line.split(".")[1] for line in group.splitlines()} for group in groups]
    assert skills == [{name} for name in SKILL_NAMES]


def test_a_project_skill_file_shows_its_lines_and_its_path(project: Path) -> None:
    project_skill(project, "python/tests.md", TESTS)
    result = show(project, "skill_files.python.tests.md")
    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "skill_files.python.tests.md = (42 lines)   project .orbit/skills/python/tests.md\n"
    )


def test_full_prints_a_skill_files_text(project: Path) -> None:
    project_skill(project, "python/tests.md", TESTS)
    result = show(project, "skill_files.python.tests.md", "--full")
    assert result.stdout == (
        f'skill_files.python.tests.md = """\n{TESTS}"""   project .orbit/skills/python/tests.md\n'
    )


def test_a_skills_key_shows_only_its_files(project: Path) -> None:
    result = show(project, "skill_files.python")
    assert result.exit_code == 0, result.output
    keys = [line.split(" = ")[0] for line in result.stdout.splitlines()]
    assert keys == [f"skill_files.python.{path}" for path in shipped_files("python")]


def test_each_mapping_is_listed_field_by_field_from_the_project_file(project: Path) -> None:
    mapped_paths(project)
    project_file(project, MAPPINGS)
    result = show(project, "skills")
    assert result.exit_code == 0, result.output
    assert result.stdout == (
        'skills.1.paths = ["src", "noxfile.py"]   project .orbit/config.toml\n'
        'skills.1.use = ["python"]   project .orbit/config.toml\n'
        'skills.2.paths = ["native"]   project .orbit/config.toml\n'
        'skills.2.use = ["cpp"]   project .orbit/config.toml\n'
    )


def test_json_nests_skill_files_by_skill_keeping_each_path_whole(project: Path) -> None:
    path = project_skill(project, "python/tests.md", TESTS)
    result = show(project, "skill_files", "--json")
    assert result.exit_code == 0, result.output
    python = json.loads(result.stdout)["skill_files"]["python"]
    assert python["tests.md"] == {"value": "(42 lines)", "layer": "project", "file": str(path)}
    skill_md = counted(shipped_files("python")["SKILL.md"])
    assert python["SKILL.md"] == {"value": skill_md, "layer": "package", "file": None}
    assert list(python) == list(shipped_files("python"))


def test_json_with_full_gives_a_skill_files_text(project: Path) -> None:
    path = project_skill(project, "python/tests.md", TESTS)
    result = show(project, "skill_files.python.tests.md", "--json", "--full")
    tests = json.loads(result.stdout)["skill_files"]["python"]["tests.md"]
    assert tests == {"value": TESTS, "layer": "project", "file": str(path)}


def test_json_gives_the_same_values_and_layers(project: Path, config_dir: Path) -> None:
    path = project_file(project, '[roles.reviewer]\nmodel = "sonnet"\n')
    user_path = user_file(config_dir, '[roles.planner]\nmodel = "opus"\n')
    result = show(project, "roles", "--json")
    assert result.exit_code == 0, result.output
    roles = json.loads(result.stdout)["roles"]
    assert roles["reviewer"]["model"] == {"value": "sonnet", "layer": "project", "file": str(path)}
    assert roles["planner"]["model"] == {"value": "opus", "layer": "user", "file": str(user_path)}
    assert roles["coder"]["tools"]["workspace"]["layer"] == "package"
    assert roles["coder"]["tools"]["workspace"]["file"] is None
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


def test_a_key_ending_partway_through_a_skill_files_path_exits_2_naming_it(
    project: Path,
) -> None:
    project_skill(project, "python/tests.md", TESTS)
    result = show(project, "skill_files.python.tests")
    assert result.exit_code == 2
    assert "No setting skill_files.python.tests in orbit's configuration." in result.stderr
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
    assert keys == [f"roles.qa-lead.{key}" for key in ROLE_KEYS]


def test_a_missing_workspace_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["config", "show", "-C", str(tmp_path / "nowhere")])
    assert result.exit_code == 2
    assert "doesn't exist" in result.stderr


def test_showing_writes_nothing(project: Path, tmp_path: Path) -> None:
    project_file(project, "fix_attempts = 2\n")
    before = sorted(tmp_path.rglob("*"))
    assert show(project).exit_code == 0
    assert sorted(tmp_path.rglob("*")) == before


def map_rows(table: str) -> dict[str, dict[str, str]]:
    """The cells of the skill map ``table`` prints, by skill and by role, read from its columns,
    which at least two spaces part."""
    header, *rows = (re.split(r" {2,}", line) for line in table.splitlines())
    return {skill: dict(zip(header[1:], cells, strict=True)) for skill, *cells in rows}


def test_skills_maps_each_skill_to_where_each_role_gets_it(project: Path) -> None:
    mapped_paths(project)
    project_file(project, PYTHON_MAPPING)
    result = show(project, "--skills")
    assert result.exit_code == 0, result.output
    table = result.stdout.split("\n\n")[0]
    assert re.split(r" {2,}", table.splitlines()[0]) == ["skill", *BUILT_IN_ROLE_NAMES]
    rows = map_rows(table)
    assert list(rows) == SKILL_NAMES
    lenses = len(LENS_REVIEWERS) + len(SUPER_LENS_REVIEWERS)
    assert list(rows["python"].values()) == [
        "mapped",
        "mapped",
        "task / mapped",
        "reviewed task / mapped",
        "role",
        *["reviewed task / mapped"] * lenses,
    ]
    assert list(rows["cpp"].values()) == [
        "—",
        "—",
        "task",
        "reviewed task",
        "role",
        *["reviewed task"] * lenses,
    ]
    assert rows["code-style"]["configurator"] == "task"
    assert rows["code-style"]["experimenter"] == "—"
    review_style = rows["review-style"]
    assert (review_style["style-reviewer"], review_style["drift-reviewer"]) == (
        "role",
        "reviewed task",
    )
    assert (review_style["reviewer"], review_style["bugs-reviewer"]) == ("role", "reviewed task")


def test_skills_lines_up_each_column_under_its_role(project: Path) -> None:
    mapped_paths(project)
    project_file(project, PYTHON_MAPPING)
    table = show(project, "--skills").stdout.split("\n\n")[0].splitlines()
    starts = [match.start() for match in re.finditer(r"(?<=  )\S", table[0])]
    for line in table[1:]:
        assert [match.start() for match in re.finditer(r"(?<=  )\S", line)] == starts


def test_skills_ends_with_a_line_saying_what_each_value_means(project: Path) -> None:
    result = show(project, "--skills")
    assert result.stdout.endswith(f"\n\n{LEGEND}\n")


def test_skills_with_json_gives_each_skills_cells_by_role(project: Path) -> None:
    mapped_paths(project)
    project_file(project, PYTHON_MAPPING)
    result = show(project, "--skills", "--json")
    assert result.exit_code == 0, result.output
    skills = json.loads(result.stdout)["skills"]
    assert list(skills) == SKILL_NAMES
    assert skills["cpp"] == {
        "planner": "—",
        "experimenter": "—",
        "coder": "task",
        "reviewer": "reviewed task",
        "configurator": "role",
        **dict.fromkeys(LENS_REVIEWERS, "reviewed task"),
        **dict.fromkeys(SUPER_LENS_REVIEWERS, "reviewed task"),
    }
    assert skills["python"]["reviewer"] == "reviewed task / mapped"


@pytest.mark.parametrize("args", [["roles"], ["--full"], ["server.port", "--json"]])
def test_skills_with_a_key_or_full_exits_2_saying_it_shows_the_whole_map(
    project: Path, args: list[str]
) -> None:
    result = show(project, "--skills", *args)
    assert result.exit_code == 2
    assert result.stderr == SKILLS_ALONE
    assert result.stdout == ""


def test_skills_with_an_invalid_configuration_exits_2_naming_its_file(project: Path) -> None:
    path = project_file(project, "fix_attempts = 0\n")
    result = show(project, "--skills")
    assert result.exit_code == 2
    assert result.stderr.startswith(f"{INVALID_CONFIG}{path}: ")
    assert result.stdout == ""


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


def test_a_skill_file_of_one_line_says_so() -> None:
    report = {"skill_files.x.SKILL.md": Setting(SkillFileText("One.\n"), PACKAGE)}
    text = config_text(report, full=False, root=Path("/"), home=Path("/home"))
    assert text == "skill_files.x.SKILL.md = (1 line)   package"


def test_full_prints_a_skill_file_without_a_last_line_break_as_it_is() -> None:
    report = {"skill_files.x.SKILL.md": Setting(SkillFileText("One.\nTwo."), PACKAGE)}
    text = config_text(report, full=True, root=Path("/"), home=Path("/home"))
    assert text == 'skill_files.x.SKILL.md = """\nOne.\nTwo.\n"""   package'


def test_json_keeps_a_skill_files_path_in_folders_whole() -> None:
    report = {
        "skill_files.cpp.scripts/run.sh": Setting(SkillFileText("run\n"), PACKAGE),
        "skills.1.use": Setting(("cpp",), DEFAULT),
    }
    tree = json.loads(config_json(report, full=False))
    assert tree == {
        "skill_files": {
            "cpp": {"scripts/run.sh": {"value": "(1 line)", "layer": "package", "file": None}}
        },
        "skills": {"1": {"use": {"value": ["cpp"], "layer": "default", "file": None}}},
    }


ROLES_MAP = SkillMap(
    roles=("planner", "reviewer"),
    cells={
        "code-style": {"planner": "role", "reviewer": "role"},
        "python": {"planner": "mapped", "reviewer": "reviewed task / mapped"},
    },
)
"""A skill map of two roles and two skills."""


def test_the_skill_map_prints_its_cells_in_columns_under_its_roles() -> None:
    assert skill_map_text(ROLES_MAP).split("\n\n")[0] == (
        "skill        planner   reviewer\n"
        "code-style   role      role\n"
        "python       mapped    reviewed task / mapped"
    )


def test_a_skill_map_without_skills_prints_its_header_and_legend() -> None:
    assert skill_map_text(SkillMap(roles=("coder",), cells={})) == f"skill   coder\n\n{LEGEND}"


def test_the_skill_map_as_json_nests_cells_by_skill_and_role() -> None:
    cells = {"cpp": {"coder": "task", "planner": "—"}}
    assert json.loads(skill_map_json(SkillMap(roles=("coder", "planner"), cells=cells))) == {
        "skills": cells
    }
    assert "—" in skill_map_json(SkillMap(roles=("planner",), cells={"cpp": {"planner": "—"}}))
