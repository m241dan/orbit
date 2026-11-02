"""The built-in roles, read from the roles.toml and Markdown files shipped with orbit."""

import re
import tomllib
from collections.abc import Collection
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from orbit.agent.application.skill_delivery import SkillDelivery
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.external.built_in_roles import read_built_in_roles, read_instruction_files
from orbit.agent.external.skill_files import SKILL_DIRECTORY, read_skill_library, skill_names
from orbit.protocol import (
    BUILT_IN_ROLE_NAMES,
    LENS_REVIEWERS,
    PINNED_ROLE_NAMES,
    SUPER_LENS_REVIEWERS,
)

FOLDER = files("orbit.agent.external") / "instructions"
README = Path(__file__).parent.parent / "README.md"
INSTRUCTION_FILES = {
    **{name: f"{name}.md" for name in PINNED_ROLE_NAMES},
    **dict.fromkeys(LENS_REVIEWERS, "lens-reviewer.md"),
    **dict.fromkeys(SUPER_LENS_REVIEWERS, "super-lens-reviewer.md"),
}
"""The Markdown file each built-in role's instructions are in: its own, the per-commit lens
reviewers' shared one, or the super review's lens reviewers' shared one."""
ROLES_TOML = (FOLDER / "roles.toml").read_text(encoding="utf-8")

REVIEW_TOOLS = ("get_task", "complete_task", "report_progress", "give_up", "suggest_task")
MERGE_PASS_TOOLS = (*REVIEW_TOOLS, "dismiss_finding")
LENS_TOOLS = ("get_task", "complete_task", "report_progress", "give_up", "report_finding")
LENS_FILE = "lens-reviewer.md"
"""The instructions file every per-commit lens reviewer shares."""
SUPER_LENS_FILE = "super-lens-reviewer.md"
"""The instructions file every lens reviewer of the super review shares."""
REVIEWER_SKILLS = (
    "code-style",
    "security",
    "review-fit",
    "review-bugs",
    "review-tests",
    "review-logic",
    "review-security",
    "review-performance",
    "review-style",
    "review-drift",
    "review-scaling",
)
BUILT_IN_FIELDS: dict[str, dict[str, Any]] = {
    "coder": {
        "model": None,
        "tools": {
            "orbit": ("get_task", "complete_task", "run_check", "report_progress", "give_up"),
            "workspace": "shell",
            "web": True,
        },
        "blocking_findings": False,
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
        "tools": {"orbit": MERGE_PASS_TOOLS, "workspace": "read", "web": True},
        "blocking_findings": False,
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
    "planner": {
        "model": None,
        "tools": {
            "orbit": (
                "propose_task",
                "report_progress",
                "save_report",
                "list_reports",
                "read_report",
            ),
            "workspace": "read",
            "web": True,
        },
        "blocking_findings": False,
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
        "tools": {
            "orbit": ("report_progress", "save_report", "list_reports", "read_report"),
            "workspace": "shell",
            "web": True,
        },
        "blocking_findings": False,
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
        "tools": {
            "orbit": ("get_task", "propose_checks", "report_progress", "give_up"),
            "workspace": "read",
            "web": True,
        },
        "blocking_findings": False,
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
        "skills": ("python", "cpp"),
    },
}
"""Each built-in role's fields but its instructions: those orbit has always had, where it
works, which lane runs it, whether you talk to it, whether its work merges, no effort, and the
skills its agents get."""

LENS_SKILLS = {
    "fit-reviewer": ("code-style", "review-fit"),
    "bugs-reviewer": ("code-style", "review-bugs"),
    "tests-reviewer": ("code-style", "review-tests"),
    "logic-reviewer": ("code-style", "review-logic"),
    "security-reviewer": ("code-style", "security", "review-security"),
    "performance-reviewer": ("code-style", "review-performance"),
}
"""Each lens reviewer's skills: code-style and its lens, and security for the security lens."""

BLOCKING_LENSES = frozenset({"bugs-reviewer", "security-reviewer"})
"""The lens reviewers whose findings may block merges."""

BUILT_IN_FIELDS |= {
    name: BUILT_IN_FIELDS["reviewer"]
    | {
        "tools": {"orbit": LENS_TOOLS, "workspace": "read", "web": True},
        "skills": skills,
        "blocking_findings": name in BLOCKING_LENSES,
    }
    for name, skills in LENS_SKILLS.items()
}

SUPER_LENS_SKILLS = {
    "style-reviewer": ("code-style", "review-style"),
    "drift-reviewer": ("code-style", "review-drift"),
    "scaling-reviewer": ("code-style", "review-scaling"),
}
"""Each lens reviewer of the super review's skills: code-style and its lens."""

BUILT_IN_FIELDS |= {
    name: BUILT_IN_FIELDS["fit-reviewer"] | {"model": "opus", "skills": skills}
    for name, skills in SUPER_LENS_SKILLS.items()
}


def role_directory(tmp_path: Path, roles_toml: str, leave_out: Collection[str] = ()) -> Path:
    """``tmp_path`` holding ``roles_toml`` and the shipped Markdown files but those named in
    ``leave_out``."""
    (tmp_path / "roles.toml").write_text(roles_toml, encoding="utf-8")
    for file in set(INSTRUCTION_FILES.values()) - set(leave_out):
        text = (FOLDER / file).read_text(encoding="utf-8")
        (tmp_path / file).write_text(text, encoding="utf-8")
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


def test_there_is_one_markdown_file_per_pinned_role_and_one_each_kind_of_lens_shares() -> None:
    names = {entry.name for entry in FOLDER.iterdir() if entry.name.endswith(".md")}
    assert names == {f"{name}.md" for name in PINNED_ROLE_NAMES} | {LENS_FILE, SUPER_LENS_FILE}


def test_each_built_in_role_is_told_its_file() -> None:
    roles = read_built_in_roles()
    assert tuple(roles) == BUILT_IN_ROLE_NAMES
    for name, role in roles.items():
        text = (FOLDER / INSTRUCTION_FILES[name]).read_text(encoding="utf-8")
        assert role.instructions == text.strip()


def test_roles_toml_ships_in_the_package_naming_each_roles_markdown_file() -> None:
    tables = tomllib.loads(ROLES_TOML)["roles"]
    assert set(tables) == set(BUILT_IN_ROLE_NAMES)
    for name, table in tables.items():
        assert table["instructions"] == INSTRUCTION_FILES[name]


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
    assert roles["drift-reviewer"].model == "haiku"
    assert roles["coder"] == read_built_in_roles()["coder"]


def test_each_built_in_roles_instructions_are_in_the_file_roles_toml_names() -> None:
    assert read_instruction_files() == INSTRUCTION_FILES


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
    for name in ("reviewer", "style-reviewer", "planner"):
        assert "read and search the code" in roles[name].instructions


def test_the_configurator_looks_for_the_projects_own_checks() -> None:
    instructions = read_built_in_roles()["configurator"].instructions
    for hint in ("pre-commit", "package.json", "pyproject.toml", "Makefile", "quick", "setup"):
        assert hint in instructions
    assert "your work is reading and proposing" in instructions


def test_the_configurator_proposes_from_the_suggested_checks_of_the_skills_that_fit() -> None:
    instructions = read_built_in_roles()["configurator"].instructions
    assert "Suggested checks of the language skills that fit the project" in instructions


def test_the_configurator_sets_up_the_include_checker_before_proposing_it() -> None:
    instructions = " ".join(read_built_in_roles()["configurator"].instructions.split())
    assert "`python3 tools/check_includes.py`: copy `cpp`'s `scripts/check_includes.py`" in (
        instructions
    )
    assert "to `tools/check_includes.py`, and write `include-rules.toml`" in instructions
    assert "with the user" in instructions


def test_the_configurator_is_launched_with_the_python_and_cpp_skills(tmp_path: Path) -> None:
    library = read_skill_library(str(tmp_path))
    delivery = SkillDelivery(library, (), lambda agent_id: None)
    told = delivery.instructions(read_built_in_roles()["configurator"], None)
    for name in ("python", "cpp"):
        assert library.skills[name].body.rstrip() in told, name


def test_the_readme_lists_each_built_in_roles_tools() -> None:
    for name, role in read_built_in_roles().items():
        assert readme_column(name, 2) == role.tools.orbit, name


def test_the_readme_lists_each_built_in_roles_workspace_level() -> None:
    for name, role in read_built_in_roles().items():
        assert readme_column(name, 4)[-1] == role.workspace_level, name
        assert role.web, name


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


LENS_REVIEWER_REPORTS_FINDINGS = (
    "Read the code around the diff as well as the diff itself, so you judge each line in"
    " context: what looks odd on its own may be the project's convention.",
    "A task made for a plan opens its prompt with `Plan: <path>`, the plan's record: a"
    " requirements doc saying why the change was wanted and what was decided. Read it.",
    "Look only through your `review-*` lens skill.",
    "Report each problem worth fixing with `report_finding`:",
    "**body**: where it is (file and function or line), the input or scenario that shows it, and"
    " why it matters.",
    "**severity**: `blocking` only when your lens skill says such a finding blocks merges",
    "Drop a finding that a queued task or a commit landed since already covers.",
    "Don't file tasks: you report findings, and the merge pass files them.",
    "a lens that finds nothing reports nothing",
    "Then call `complete_task` with one line giving how many findings you reported and how many"
    ' of them block, such as "2 findings, 1 blocking", "1 finding" or "Clean".',
)
MERGE_PASS_FILES_AND_DISMISSES = (
    "Your task hands you the findings of one review's lenses.",
    "Check each finding against the code",
    "Drop a finding that a queued task or a commit landed since already covers",
    "Group related findings into one task: problems in the same function or file, or a bug"
    " together with the test that would have caught it, even when different lenses reported"
    " them.",
    "Rank the tasks by severity and file the most severe within your cap. For each, call"
    " `suggest_task` with:",
    "it quotes each reviewed commit's message under `Reviewed commit <id>:`; the message's body"
    " is the prompt the coder was given, so you know what was asked. A super review or a set"
    " report reviews no task's commit, so its task quotes none.",
    "**Where**: the `Plan: <path>` line of the reviewed commit's message when it has one, then"
    " the files and functions involved.",
    "**Change**: what to do, with the input or scenario that shows the problem.",
    "**Keep**: what must stay as it is.",
    "**Done**: the behaviours tests must pin down.",
    "**findings**: the ids of the findings the task covers, the main one first.",
    "**severity**: `blocking` only for a blocking finding that holds up once checked",
    "Confirm how a framework behaves before asking for a test that must fail without the fix, and"
    " write Done items that can all hold at once.",
    "Dismiss every finding you don't file with `dismiss_finding` and a short reason, such as"
    " `over the cap`, `covered by task 12` or `doesn't hold: <why>`.",
    "Then call `complete_task` with one line giving how many tasks you filed from how many"
    ' findings, and how many you dismissed, such as "3 tasks from 5 findings, 2 dismissed".',
)
REVIEWER_RETIRED_PHRASES = (
    "**Clarity**",
    "Clarity improvements join a task",
    "Until orbit can hold merges",
    "Look at the change through every lens you were given",
)


def reviewer_text() -> str:
    """The reviewer's instructions with every run of whitespace made a single space."""
    return " ".join(read_built_in_roles()["reviewer"].instructions.split())


@pytest.mark.parametrize("name", LENS_REVIEWERS)
def test_each_lens_reviewer_reports_findings_through_its_one_lens(name: str) -> None:
    text = role_text(name)
    for passage in LENS_REVIEWER_REPORTS_FINDINGS:
        assert passage in text, passage


def test_the_reviewer_as_merge_pass_files_the_findings_that_hold_and_dismisses_the_rest() -> None:
    text = reviewer_text()
    for passage in MERGE_PASS_FILES_AND_DISMISSES:
        assert passage in text, passage


def test_the_reviewer_no_longer_has_a_built_in_clarity_lens() -> None:
    text = reviewer_text()
    for phrase in REVIEWER_RETIRED_PHRASES:
        assert phrase not in text, phrase


SUPER_LENS_LOOKS_THROUGH_ITS_LENS = (
    "A super review hands you a read-only snapshot of the codebase at its latest commit, which has"
    " passed the project's checks; orbit asks for one every few commits, or when the user does."
    " The task lists the commits landed since the last super review, the files they changed, and"
    " a sample of other code files orbit chose so that, review by review, every code file gets"
    " read.",
    "Start with the shape of the project: its README, its configuration, its package layout and"
    " its own guidance (CLAUDE.md and the like), which says what structure the project means to"
    " keep. Then read the files changed since the last super review and the sampled ones, and"
    " follow what you find into the code around them.",
    "The project's plans are recorded in numbered requirements docs, under `docs/` or wherever"
    " the project keeps them. Take one search over their titles and Goal lines to learn what each"
    " covers, and read a record in full only when a finding touches the code it covers, before"
    " you report anything about it. Code that no longer does what its record agreed, with no"
    " later record explaining the change, is drift: the record is what was agreed, so the code is"
    " the suspect, unless the user-facing docs say the behaviour changed on purpose.",
    "Look only through your `review-*` lens skill. It owns one question and says what to look for"
    " and what it measures against. A problem another lens owns is that lens's to report: leave"
    " it. The checks pass and every commit has had its own review, so look for what only shows"
    " across commits and across the codebase.",
    "When the task is a review of fixes, check whether the findings your lens raised are fixed",
    "Report what still isn't fixed",
    "Don't file tasks: you report findings, and the merge pass files them.",
    "favouring those that remove a whole class of problems, such as one shared implementation in"
    " place of several copies, over those that fix a single instance.",
    "Report each with `report_finding`:",
)
SUPER_LENS_RETIRED_PHRASES = (
    "**Latent bugs**",
    "**Structure that no longer fits**",
    "call suggest_task",
    "Your lenses are your `review-*` skills.",
)
LENS_QUESTIONS = {
    "review-style": "**Your question:** across the commits since the last super review and the"
    " sampled files, do names, docs, comments and function shape follow the conventions, beyond"
    " what the checks enforce?",
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


@pytest.mark.parametrize("name", SUPER_LENS_REVIEWERS)
def test_each_super_lens_looks_through_its_one_lens_at_what_landed_and_reports(name: str) -> None:
    text = role_text(name)
    for passage in SUPER_LENS_LOOKS_THROUGH_ITS_LENS:
        assert passage in text, passage


@pytest.mark.parametrize("name", SUPER_LENS_REVIEWERS)
def test_a_super_lens_neither_files_tasks_nor_looks_through_every_lens(name: str) -> None:
    text = role_text(name)
    for phrase in SUPER_LENS_RETIRED_PHRASES:
        assert phrase not in text, phrase


def lens_text(name: str) -> str:
    """The lens skill ``name``'s ``SKILL.md`` with every run of whitespace made a single space."""
    return " ".join((SKILL_DIRECTORY / name / "SKILL.md").read_text(encoding="utf-8").split())


@pytest.mark.parametrize("name", LENS_QUESTIONS)
def test_the_super_review_lenses_ask_across_the_commits_since_and_the_sample(name: str) -> None:
    assert LENS_QUESTIONS[name] in lens_text(name)


def test_the_style_lens_reads_the_sampled_files_rather_than_leaving_older_code() -> None:
    assert "older code waits for its own tasks" not in lens_text("review-style")


def test_the_coder_keeps_tests_independent_as_code_style_says() -> None:
    text = role_text("coder")
    assert text.endswith(CODER_DEFERS_TEST_ISOLATION)
    assert "isolated from real files" not in text


def test_the_configurator_proposes_slower_checks_as_sets_with_triggers() -> None:
    instructions = read_built_in_roles()["configurator"].instructions
    for hint in ("task checks", "named sets", "every 10 commits", "holds merges", "by hand"):
        assert hint in instructions
