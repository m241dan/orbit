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
from orbit.agent.external.skill_files import SKILL_DIRECTORY, skill_names
from orbit.protocol import BUILT_IN_ROLE_NAMES

FOLDER = files("orbit.agent.external") / "instructions"
README = Path(__file__).parent.parent / "README.md"
ROLES_TOML = (FOLDER / "roles.toml").read_text(encoding="utf-8")

REVIEW_TOOLS = ("get_task", "complete_task", "report_progress", "give_up", "suggest_task")
REVIEWER_SKILLS = (
    "code-style",
    "security",
    "review-fit",
    "review-bugs",
    "review-tests",
    "review-logic",
    "review-security",
    "review-performance",
)
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
        "consults": (),
        "consult_limit_per_hour": 6,
        "copy_ttl_hours": 24,
        "skills": ("code-style", "security"),
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
        "consults": (),
        "consult_limit_per_hour": 6,
        "copy_ttl_hours": 24,
        "skills": REVIEWER_SKILLS,
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
        "consults": (),
        "consult_limit_per_hour": 6,
        "copy_ttl_hours": 24,
        "skills": ("code-style", "review-style", "review-drift", "review-scaling"),
    },
    "planner": {
        "model": None,
        "tool_access": "restricted",
        "tools": ("propose_task", "report_progress", "save_report", "list_reports", "read_report"),
        "verify": False,
        "timeout_minutes": None,
        "lane": "planner",
        "workspace": "checkout",
        "conversation": True,
        "merge": False,
        "enabled": True,
        "effort": None,
        "runtime": "claude",
        "consults": ("experimenter",),
        "consult_limit_per_hour": 6,
        "copy_ttl_hours": 24,
        "skills": ("code-style",),
    },
    "experimenter": {
        "model": "sonnet",
        "tool_access": "full",
        "tools": ("report_progress", "save_report", "list_reports", "read_report"),
        "verify": False,
        "timeout_minutes": 30,
        "lane": "experimenter",
        "workspace": "copy",
        "conversation": True,
        "merge": False,
        "enabled": True,
        "effort": None,
        "runtime": "claude",
        "consults": (),
        "consult_limit_per_hour": 6,
        "copy_ttl_hours": 24,
        "skills": (),
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
        "consults": (),
        "consult_limit_per_hour": 6,
        "copy_ttl_hours": 24,
        "skills": (),
    },
}
"""Each built-in role's fields but its instructions: those orbit has always had, where it
works, which lane runs it, whether you talk to it, whether its work merges, no effort, and the
skills its agents get."""


def role_directory(tmp_path: Path, roles_toml: str, leave_out: Collection[str] = ()) -> Path:
    """``tmp_path`` holding ``roles_toml`` and the shipped Markdown files but those named in
    ``leave_out``."""
    (tmp_path / "roles.toml").write_text(roles_toml, encoding="utf-8")
    for name in BUILT_IN_ROLE_NAMES:
        if f"{name}.md" not in leave_out:
            text = (FOLDER / f"{name}.md").read_text(encoding="utf-8")
            (tmp_path / f"{name}.md").write_text(text, encoding="utf-8")
    return tmp_path


def readme_column(name: str, column: int) -> tuple[str, ...]:
    """The names in backticks README.md's built-in roles table lists for the role ``name`` in its
    ``column``, counting the role's own as 1."""
    row = next(
        line
        for line in README.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"| `{name}` |")
    )
    return tuple(re.findall(r"`([^`]+)`", row.split("|")[column]))


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
        assert readme_column(name, 2) == role.tools, name


def test_the_readme_lists_each_built_in_roles_skills() -> None:
    for name, role in read_built_in_roles().items():
        assert readme_column(name, 5) == role.skills, name


def test_each_built_in_roles_skills_are_skills_orbit_ships() -> None:
    shipped = set(skill_names())
    for name, role in read_built_in_roles().items():
        assert set(role.skills) <= shipped, name


PLANNER_ASKS_BEFORE_PROPOSING = (
    "Before posting the requirements, read the code each likely task touches and walk each task"
    " as its coder would: the states it passes through, what happens before and after it, what"
    " fails and what a test would pin down. Any behaviour a prompt would have to state that no"
    " answer settles is still open: ask it in a last round.",
    "Mark a requirement as following only when it follows from an answer, an earlier record or"
    " the code.",
    "A prompt carries only confirmed decisions, including those under Decided for you, and facts"
    " from the code. An implementation detail that turns up while writing one is yours: settle"
    " it, and name it in the line that closes your proposals. Something users would see or rely"
    " on that turns up goes back to the user as a question before anything is proposed.",
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


PLANNER_DECIDES_IMPLEMENTATION = (
    "Facts are your job, behaviour is the user's to decide, and the implementation is yours. Look"
    " up anything the code or the web can answer. Put to the user what users see or rely on:"
    " behaviour, edge cases, what happens on failure, interactions with existing features, stored"
    " data, and scope, including any new dependency; ask, too, about implementation that is hard"
    " to undo: a stored format or migration, a wire or protocol change, a public API. Decide the"
    " rest yourself, following the project's guidance and skills: where code goes, its structure"
    " and names, how the work splits into tasks, and user-visible wording.",
    "- **Wording**: user-visible names, keys, flags and messages, which you choose.",
    "Once a behaviour is settled, put its edge cases and their outcomes in a round of their own: a"
    " numbered table of `Given … → outcome` rows, each with the outcome you propose, which the"
    " user accepts whole or overrides by row number. An edge case with two plausible outcomes and"
    " no obvious one is a full question instead.",
    "An implementation detail that no answer settles is yours to decide.",
    "A fully specified goal has an empty frontier: say so. Once the frontier is empty, post in one"
    " message the requirements, a numbered list of one-line decisions, each ending with the"
    ' options rejected and why in a clause ("over B: <trade-off>"); **Decided for you**, the'
    " choices you made yourself, numbered on from the requirements and in the same form; and the"
    " outline. Mark a requirement as following only when it follows from an answer, an earlier"
    " record or the code. Wait for the user to confirm; when they change something, post the"
    " revised whole.",
    "When the user asks for changes, post the whole revised message. Propose once the user agrees.",
)
PLANNER_RETIRED_PHRASES = (
    "put every choice to the user",
    "a choice of your own is a question",
    "never settle it in a draft",
    "post the whole revised outline",
)


def test_the_planner_asks_about_behaviour_and_decides_the_implementation() -> None:
    text = planner_text()
    for passage in PLANNER_DECIDES_IMPLEMENTATION:
        assert passage in text, passage


def test_the_planner_no_longer_puts_every_choice_to_the_user() -> None:
    text = planner_text()
    for phrase in PLANNER_RETIRED_PHRASES:
        assert phrase not in text, phrase


PLANNER_CONSULTS = (
    "When a fact can only be found by running code, such as how a library behaves, consult a"
    " role you may consult with `consult`, saying in the same message what you're finding out"
    " and why, and carry on with what you can meanwhile; use the reply when it arrives. Ask the"
    " user instead only for facts about their own machine or accounts beyond the consultant's"
    " copy. Save a report with `save_report` when a finding will matter beyond this"
    " conversation."
)


def test_the_planner_consults_rather_than_asking_the_user_to_run_code() -> None:
    text = planner_text()
    assert PLANNER_CONSULTS in text
    assert "ask the user to run it and paste the output" not in text


def test_the_experimenter_answers_from_a_throwaway_copy() -> None:
    text = " ".join(read_built_in_roles()["experimenter"].instructions.split())
    for passage in (
        "throwaway copy of the project that is never merged",
        "change nothing beyond what that needs",
        "your last message is what the asker receives",
        "save a report with `save_report`",
    ):
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


PLANNER_GIVES_SKILLS = (
    "Give each task the skills its coder needs with propose_task's `skills`, chosen from the"
    " catalogue under Skills you can give tasks: the language skill for the code it changes, the"
    " kind skill for what it changes (such as `application`), and the project's own skills that"
    " bear on it. The coder's role brings its own skills, so name only what the task adds. Orbit"
    " passes the list on as it is, and reviews and follow-up tasks inherit it."
)


def test_the_planner_gives_each_task_the_skills_its_coder_needs() -> None:
    text = planner_text()
    gives = text.index(PLANNER_GIVES_SKILLS)
    done = text.index("the coder chooses test names and files to match the project's style.")
    assert done < gives < text.index("Write each prompt against the code")


PLANNER_FIXES_DRAFTS = (
    "Proposals are drafts the user accepts, edits or removes on the task list. Until the user"
    " accepts one, you can fix it: `edit_draft`, `move_draft` and `remove_draft` change your own"
    " drafts, and `list_drafts` and `read_draft` show them as they stand. Once accepted or edited"
    " by the user, a draft is theirs; take the user's words as the truth about what it says. When"
    " you find a mistake in a draft, or the user changes their mind about one, fix the draft"
    " rather than asking the user to. For one the user has accepted, name it to remove and"
    " propose its replacement."
)


def test_the_planner_fixes_its_own_drafts_until_the_user_accepts_them() -> None:
    text = planner_text()
    assert PLANNER_FIXES_DRAFTS in text
    assert "you can't change or remove them" not in text


REVIEWER_LOOKS_THROUGH_ITS_LENSES = (
    "It also lists the tasks already queued or running, and the commits landed since the one you"
    " review: don't suggest what they cover.",
    "Your lenses are your `review-*` skills. Each owns one question and says what to look for,"
    " what it measures against and what counts as blocking. Look at the change through every lens"
    " you were given. Most changes hold up under most lenses, and a lens that finds nothing"
    " reports nothing.",
    "Until orbit can hold merges, a finding a lens says blocks merges is the most severe kind:"
    " suggest it first, and say in its prompt why it would block.",
    "Before suggesting, drop a finding that a queued task or a commit landed since already covers,"
    " and where a landed commit's subject suggests it changed the code you're judging, say so in"
    " the suggestion so the coder checks it still applies. Confirm how a framework behaves before"
    " asking for a test that must fail without the fix, and write Done items that can all hold at"
    " once.",
)
REVIEWER_RETIRED_PHRASES = ("**Clarity**", "Clarity improvements join a task")


def reviewer_text() -> str:
    """The reviewer's instructions with every run of whitespace made a single space."""
    return " ".join(read_built_in_roles()["reviewer"].instructions.split())


def test_the_reviewer_looks_through_its_lens_skills_with_blocking_findings_first() -> None:
    text = reviewer_text()
    for passage in REVIEWER_LOOKS_THROUGH_ITS_LENSES:
        assert passage in text, passage


def test_the_reviewer_no_longer_has_a_built_in_clarity_lens() -> None:
    text = reviewer_text()
    for phrase in REVIEWER_RETIRED_PHRASES:
        assert phrase not in text, phrase


SUPER_REVIEWER_LOOKS_THROUGH_ITS_LENSES = (
    "A read-only snapshot of the codebase at its latest commit, which has passed the project's"
    " checks; orbit asks for this every few commits, or when the user does. The task lists the"
    " commits landed since the last super review, the files they changed, and a sample of other"
    " code files orbit chose so that, review by review, every code file gets read.",
    "Start with the shape of the project: its README, its configuration, its package layout and"
    " its own guidance (CLAUDE.md and the like), which says what structure the project means to"
    " keep. Then read the files changed since the last super review and the sampled ones, and"
    " follow what you find into the code around them.",
    "The project's plans are recorded in numbered requirements docs, under `docs/` or wherever"
    " the project keeps them. Take one search over their titles and Goal lines to learn what each"
    " covers, and read a record in full only when a finding touches the code it covers, before"
    " you suggest anything about it. Code that no longer does what its record agreed, with no"
    " later record explaining the change, is drift: the record is what was agreed, so the code is"
    " the suspect, unless the user-facing docs say the behaviour changed on purpose.",
    "Your lenses are your `review-*` skills. Each owns one question and says what to look for and"
    " what it measures against. The checks pass and every commit has had its own review, so look"
    " through each lens for what only shows across commits and across the codebase. A lens that"
    " finds nothing reports nothing.",
    "Before suggesting, drop a finding that a queued task already covers. Confirm how a framework"
    " behaves before asking for a test that must fail without the fix, and write Done items that"
    " can all hold at once.",
)
SUPER_REVIEWER_RETIRED_PHRASES = ("**Latent bugs**", "**Structure that no longer fits**")
LENS_QUESTIONS = {
    "review-drift": "**Your question:** what has crept into the structure across the commits"
    " since the last super review and the sampled files?",
    "review-scaling": "**Your question:** across the commits since the last super review and the"
    " sampled files, what cost has built up?",
}
CODER_DEFERS_TEST_ISOLATION = (
    "Cover the error paths and edge cases the task names as well as the happy path, and keep tests"
    " independent of the machine and of each other, as `code-style` says."
)


def role_text(name: str) -> str:
    """The instructions of the role ``name`` with every run of whitespace made a single space."""
    return " ".join(read_built_in_roles()[name].instructions.split())


def test_the_super_reviewer_looks_through_its_lens_skills_at_what_landed_and_the_sample() -> None:
    text = role_text("super-reviewer")
    for passage in SUPER_REVIEWER_LOOKS_THROUGH_ITS_LENSES:
        assert passage in text, passage


def test_the_super_reviewer_no_longer_has_a_built_in_list_of_lenses() -> None:
    text = role_text("super-reviewer")
    for phrase in SUPER_REVIEWER_RETIRED_PHRASES:
        assert phrase not in text, phrase


@pytest.mark.parametrize("name", LENS_QUESTIONS)
def test_the_super_review_lenses_ask_across_the_commits_since_and_the_sample(name: str) -> None:
    skill = (SKILL_DIRECTORY / name / "SKILL.md").read_text(encoding="utf-8")
    assert LENS_QUESTIONS[name] in " ".join(skill.split())


def test_the_coder_keeps_tests_independent_as_code_style_says() -> None:
    text = role_text("coder")
    assert text.endswith(CODER_DEFERS_TEST_ISOLATION)
    assert "isolated from real files" not in text
