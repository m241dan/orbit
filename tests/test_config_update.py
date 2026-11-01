"""What ``orbit init --update`` adds to a project file from the reference config: the keys a
table it has doesn't mention, after the table's last line, and the tables it lacks, at the end
under a dated line, never changing, moving or removing a line it had; and the lines ``orbit init
--roles`` adds, pointing each built-in role at its copy of its instructions; and the checks the
configurator writes, in place of a file's steps and setup, or of the reference's examples of
them."""

import tomllib
from datetime import date

import pytest

from orbit.agent.domain.checks import ChecksProposal, Setup, VerificationSet
from orbit.agent.domain.config import Package, load_layered
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.config_update import (
    Additions,
    ConfigUpdate,
    InstructionLines,
    InstructionsUpdate,
    add_instructions,
    instructions_setting,
    reference_with_checks,
    update_text,
    with_checks,
)
from orbit.agent.domain.roles import built_in_roles
from orbit.agent.domain.triggers import ChecksTrigger
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.agent.external.built_in_triggers import read_built_in_triggers
from orbit.protocol import BUILT_IN_ROLE_NAMES

TODAY = date(2026, 10, 25)
INSTRUCTIONS = {name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES}

ADDED = "# Added by orbit init --update on 2026-10-25: settings this file didn't mention."

REFERENCE = """\
# The file's header.

# How many fixes.
# fix_attempts = 5
# How many checks.
# max_checks = 20

# How the tree is set up. For example:
# [setup]
# The command.
# command = ["make"]

# The roles, by name.

# [roles.coder]
# Its model.
# model: unset
# Whether it merges.
# merge = true
"""

STEPS = """\
# The steps. For example:
# [[verification.steps]]
# Its name.
# name = "Tests"
# Its command.
# command = ["pytest"]
"""

LIMITS = "fix_attempts = 3\nmax_checks = 9\n"
SETUP = '[setup]\ncommand = ["make"]\n'
LIMIT_LINES = "# How many fixes.\n# fix_attempts = 5\n# How many checks.\n# max_checks = 20\n"
SETUP_SECTION = (
    '# How the tree is set up. For example:\n# [setup]\n# The command.\n# command = ["make"]\n'
)
CODER_SECTION = (
    "# [roles.coder]\n# Its model.\n# model: unset\n# Whether it merges.\n# merge = true\n"
)
MERGE_LINES = "# Whether it merges.\n# merge = true\n"
MERGE_ADDED = Additions(keys={"[roles.coder]": ("merge",)})


def updated(text: str, reference: str = REFERENCE) -> ConfigUpdate:
    """``text`` updated from ``reference`` on :data:`TODAY`."""
    return update_text(text, reference, TODAY)


def test_a_file_mentioning_every_setting_gets_nothing() -> None:
    assert updated(REFERENCE) == ConfigUpdate(REFERENCE, Additions())


def test_a_key_a_set_table_lacks_follows_its_last_line() -> None:
    text = f'{LIMITS}\n{SETUP}\n[roles.coder]\nmodel = "opus"\n'
    assert updated(text) == ConfigUpdate(text + MERGE_LINES, MERGE_ADDED)


def test_a_commented_key_counts_as_mentioned() -> None:
    text = f'{LIMITS}\n{SETUP}\n[roles.coder]\nmodel = "opus"\n# merge = false\n'
    assert updated(text) == ConfigUpdate(text, Additions())


def test_a_commented_table_and_its_keys_count_as_mentioned_in_either_form() -> None:
    text = f"{LIMITS}\n{SETUP}\n# [roles.coder]\n# model: unset\n#merge=true\n"
    assert updated(text) == ConfigUpdate(text, Additions())


def test_a_commented_table_gets_the_keys_it_lacks() -> None:
    text = f"{LIMITS}\n# [setup]\n\n[roles.coder]\nmerge = false\n# model: unset\n"
    expected = (
        f'{LIMITS}\n# [setup]\n# The command.\n# command = ["make"]\n\n'
        "[roles.coder]\nmerge = false\n# model: unset\n"
    )
    assert updated(text) == ConfigUpdate(expected, Additions(keys={"[setup]": ("command",)}))


def test_tables_a_file_lacks_are_appended_under_one_dated_line() -> None:
    expected = f"{LIMITS}\n{ADDED}\n\n{SETUP_SECTION}\n{CODER_SECTION}"
    assert updated(LIMITS) == ConfigUpdate(expected, Additions(tables=("[setup]", "[roles.coder]")))


def test_an_appended_table_follows_a_blank_last_line_without_another() -> None:
    text = f'{LIMITS}\n{SETUP}\n# [roles.cod]\n# model = "x"\n# merge = true\n\n'
    assert updated(text).text == f"{text}{ADDED}\n\n{CODER_SECTION}"


def test_top_level_keys_go_at_the_top_of_a_file_starting_with_a_table() -> None:
    text = f"# The setup.\n{SETUP}\n[roles.coder]\nmodel = 'x'\nmerge = true\n"
    assert updated(text) == ConfigUpdate(
        f"{LIMIT_LINES}\n{text}", Additions(keys={"": ("fix_attempts", "max_checks")})
    )


def test_top_level_keys_follow_the_files_opening_comments() -> None:
    body = f"{SETUP}\n[roles.coder]\nmodel = 'x'\nmerge = true\n"
    text = f"# My project.\n\n{body}"
    assert updated(text).text == f"# My project.\n{LIMIT_LINES}\n{body}"


def test_top_level_keys_follow_the_last_top_level_line() -> None:
    body = f"{SETUP}\n[roles.coder]\nmodel = 'x'\nmerge = true\n"
    text = f"# My project.\nmax_checks = 9\n\n{body}"
    expected = f"# My project.\nmax_checks = 9\n# How many fixes.\n# fix_attempts = 5\n\n{body}"
    assert updated(text) == ConfigUpdate(expected, Additions(keys={"": ("fix_attempts",)}))


def test_keys_go_before_the_comments_over_the_next_table_apart_from_them() -> None:
    text = f"{LIMITS}[roles.coder]\nmodel = 'x'\n# The setup, made with make.\n{SETUP}"
    expected = (
        f"{LIMITS}[roles.coder]\nmodel = 'x'\n{MERGE_LINES}\n# The setup, made with make.\n{SETUP}"
    )
    assert updated(text) == ConfigUpdate(expected, MERGE_ADDED)


def test_keys_go_after_the_comments_that_go_on_from_a_tables_last_line() -> None:
    text = f"{LIMITS}\n{SETUP}\n[roles.coder]\nmodel = 'x'\n# Until opus is back.\n\n# Notes.\n"
    expected = (
        f"{LIMITS}\n{SETUP}\n[roles.coder]\nmodel = 'x'\n# Until opus is back.\n{MERGE_LINES}"
        "\n# Notes.\n"
    )
    assert updated(text).text == expected


@pytest.mark.parametrize("quotes", ['"""', "'''"])
def test_a_multi_line_strings_lines_are_its_value_and_keys_follow_its_end(quotes: str) -> None:
    value = f"{quotes}\n[not.a.table]\n# merge = 1\n{quotes}"
    text = f"{LIMITS}\n{SETUP}\n[roles.coder]\nmodel = {value}\n"
    assert updated(text) == ConfigUpdate(text + MERGE_LINES, MERGE_ADDED)


def test_a_string_closed_on_its_own_line_leaves_the_next_lines_as_they_are() -> None:
    text = f'{LIMITS}\n{SETUP}\n[roles.coder]\nmodel = """opus"""\n# merge = true\n'
    assert updated(text) == ConfigUpdate(text, Additions())


def test_a_headers_spacing_quotes_and_comment_dont_matter() -> None:
    text = f"{LIMITS}\n{SETUP}\n[ roles . \"coder\" ]  # Ours.\nmodel = 'x'\n"
    assert updated(text) == ConfigUpdate(text + MERGE_LINES, MERGE_ADDED)


def test_a_table_set_inline_gets_nothing_as_it_has_no_lines_of_its_own() -> None:
    text = f"{LIMITS}\n{SETUP}\n[roles]\ncoder = {{ model = 'x' }}\n"
    assert updated(text) == ConfigUpdate(text, Additions())


def test_an_array_of_tables_mentions_the_keys_any_of_its_tables_sets() -> None:
    text = "[[verification.steps]]\nname = 'Lint'\ncommand = ['ruff']\n\n"
    text += "[[verification.steps]]\nname = 'Tests'\n"
    assert updated(text, STEPS) == ConfigUpdate(text, Additions())


def test_an_array_of_tables_gains_the_keys_it_lacks_after_its_last_table() -> None:
    text = "[[verification.steps]]\nname = 'Lint'\n\n[[verification.steps]]\nname = 'Tests'\n"
    expected = f'{text}# Its command.\n# command = ["pytest"]\n'
    assert updated(text, STEPS) == ConfigUpdate(
        expected, Additions(keys={"[[verification.steps]]": ("command",)})
    )


def test_a_file_without_a_final_newline_gets_one_before_what_follows() -> None:
    text = f"{LIMITS}\n{SETUP}\n[roles.coder]\nmodel = 'x'"
    assert updated(text).text == f"{text}\n{MERGE_LINES}"


def test_an_empty_file_gets_the_top_level_keys_and_every_table() -> None:
    assert updated("") == ConfigUpdate(
        f"{LIMIT_LINES}\n{ADDED}\n\n{SETUP_SECTION}\n{CODER_SECTION}",
        Additions(keys={"": ("fix_attempts", "max_checks")}, tables=("[setup]", "[roles.coder]")),
    )


def test_a_file_that_isnt_toml_is_refused() -> None:
    with pytest.raises(ConfigError):
        updated("[roles.coder\n")


@pytest.mark.parametrize(
    "text",
    [
        "",
        LIMITS,
        f"# The setup.\n{SETUP}\n[roles.coder]\nmodel = 'x'\n",
        f"{LIMITS}[roles.coder]\nmodel = 'x'\n# The setup, made with make.\n{SETUP}",
        f"{LIMITS}\n# [setup]\n\n[roles.coder]\nmerge = false\n",
    ],
)
def test_updating_again_adds_nothing(text: str) -> None:
    once = updated(text).text
    assert updated(once) == ConfigUpdate(once, Additions())


LATER = "Coming later; leave it commented out."
HEADING = f"# {LATER}"
RUNTIME_LINES = '# On a role, such as the one above: its runtime.\n# runtime = "claude"\n'
TRIGGER_SECTION = '# A trigger.\n# [triggers.each]\n# Its event.\n# on = "landed"\n'
MODEL_SECTION = '# A model.\n# [models.local]\n# Its id.\n# model = "qwen"\n'
LATER_SECTIONS = f"{RUNTIME_LINES}\n{TRIGGER_SECTION}\n{MODEL_SECTION}"
LATER_REFERENCE = f"{REFERENCE}\n{HEADING}\n\n{LATER_SECTIONS}"
CURRENT = f'{LIMITS}\n{SETUP}\n[roles.coder]\nmodel = "x"\nmerge = true\n'
LATER_ADDED = Additions(tables=("runtime", "[triggers.each]", "[models.local]"))


def later_updated(text: str) -> ConfigUpdate:
    """``text`` updated on :data:`TODAY` from :data:`LATER_REFERENCE`, whose settings from the
    line commenting out :data:`LATER` on are a later version's."""
    return update_text(text, LATER_REFERENCE, TODAY, later_heading=LATER)


def test_later_settings_a_file_lacks_are_appended_under_the_heading() -> None:
    expected = f"{CURRENT}\n{ADDED}\n\n{HEADING}\n\n{LATER_SECTIONS}"
    assert later_updated(CURRENT) == ConfigUpdate(expected, LATER_ADDED)


def test_the_table_before_the_heading_doesnt_gain_a_later_key() -> None:
    update = later_updated(CURRENT)
    assert update.additions.keys == {}
    assert update.text.startswith(f"{CURRENT}\n{ADDED}\n")


def test_a_file_lacking_only_a_later_table_gets_it_under_the_heading() -> None:
    text = LATER_REFERENCE.removesuffix(f"\n{MODEL_SECTION}")
    expected = f"{text}\n{ADDED}\n\n{HEADING}\n\n{MODEL_SECTION}"
    assert later_updated(text) == ConfigUpdate(expected, Additions(tables=("[models.local]",)))


def test_the_heading_follows_the_tables_of_this_version_appended() -> None:
    expected = (
        f"{LIMITS}\n{ADDED}\n\n{SETUP_SECTION}\n{CODER_SECTION}\n{HEADING}\n\n{LATER_SECTIONS}"
    )
    assert later_updated(LIMITS) == ConfigUpdate(
        expected, Additions(tables=("[setup]", "[roles.coder]", *LATER_ADDED.tables))
    )


def test_a_later_key_the_file_mentions_anywhere_isnt_appended() -> None:
    text = f'{CURRENT}# runtime = "kiro"\n\n{TRIGGER_SECTION}\n{MODEL_SECTION}'
    assert later_updated(text) == ConfigUpdate(text, Additions())


def test_a_later_key_alone_is_appended_under_the_heading() -> None:
    text = f"{CURRENT}\n{TRIGGER_SECTION}\n{MODEL_SECTION}"
    expected = f"{text}\n{ADDED}\n\n{HEADING}\n\n{RUNTIME_LINES}"
    assert later_updated(text) == ConfigUpdate(expected, Additions(tables=("runtime",)))


def test_a_heading_just_above_a_table_is_put_once() -> None:
    reference = f"{REFERENCE}\n{HEADING}\n{MODEL_SECTION}"
    update = update_text(CURRENT, reference, TODAY, later_heading=LATER)
    assert update.text == f"{CURRENT}\n{ADDED}\n\n{HEADING}\n\n{MODEL_SECTION}"


def test_a_file_with_every_setting_gets_neither_the_heading_nor_the_dated_line() -> None:
    assert later_updated(LATER_REFERENCE) == ConfigUpdate(LATER_REFERENCE, Additions())


@pytest.mark.parametrize("text", ["", LIMITS, CURRENT, LATER_REFERENCE.split(HEADING)[0]])
def test_updating_later_settings_again_adds_nothing(text: str) -> None:
    once = later_updated(text).text
    assert later_updated(once) == ConfigUpdate(once, Additions())
    assert once.count(HEADING) == 1


ROLES = ("coder", "reviewer")

ROLES_REFERENCE = """\
# The roles, by name.

# [roles.coder]
# Its instructions.
# instructions: orbit's own coder.md
# Whether it merges.
# merge = true

# [roles.reviewer]
# Its instructions.
# instructions: orbit's own reviewer.md

# A role of your own. For example:
# [roles.helper]
# instructions = "roles/helper.md"
"""

CODER_LINE = '# instructions = "roles/coder.md"'
REVIEWER_LINE = '# instructions = "roles/reviewer.md"'
CODER_TABLE = (
    "# [roles.coder]\n# Its instructions.\n# instructions: orbit's own coder.md\n"
    "# Whether it merges.\n# merge = true\n"
)
REVIEWER_TABLE = (
    "# [roles.reviewer]\n# Its instructions.\n# instructions: orbit's own reviewer.md\n"
)
SET_TABLES = "[roles.coder]\nmodel = 'opus'\n\n[roles.reviewer]\nmerge = false\n"


def with_instructions(text: str) -> InstructionsUpdate:
    """``text`` with the lines ``orbit init --roles`` adds for :data:`ROLES` on :data:`TODAY`."""
    return add_instructions(text, ROLES_REFERENCE, ROLES, TODAY)


def test_the_setting_names_the_roles_copy_under_the_roles_directory() -> None:
    assert instructions_setting("super-reviewer") == 'instructions = "roles/super-reviewer.md"'


def test_a_set_table_without_instructions_gets_the_line_after_its_last_line() -> None:
    expected = (
        f"[roles.coder]\nmodel = 'opus'\n{CODER_LINE}\n\n"
        f"[roles.reviewer]\nmerge = false\n{REVIEWER_LINE}\n"
    )
    assert with_instructions(SET_TABLES) == InstructionsUpdate(
        expected, InstructionLines(added=ROLES)
    )


def test_the_references_note_of_orbits_own_instructions_isnt_an_instructions_line() -> None:
    expected = ROLES_REFERENCE.replace(
        "# merge = true\n", f"# merge = true\n{CODER_LINE}\n"
    ).replace("reviewer.md\n", f"reviewer.md\n{REVIEWER_LINE}\n")
    assert with_instructions(ROLES_REFERENCE) == InstructionsUpdate(
        expected, InstructionLines(commented=ROLES)
    )


def test_a_line_under_a_commented_header_is_told_apart_from_one_under_a_set_header() -> None:
    text = "[roles.reviewer]\nmerge = false\n\n# [roles.coder]\n# merge = true\n"
    expected = (
        f"[roles.reviewer]\nmerge = false\n{REVIEWER_LINE}\n\n"
        f"# [roles.coder]\n# merge = true\n{CODER_LINE}\n"
    )
    assert with_instructions(text) == InstructionsUpdate(
        expected, InstructionLines(added=("reviewer",), commented=("coder",))
    )


def test_the_header_the_line_goes_under_decides_whether_its_set() -> None:
    text = "# [roles.coder]\n# merge = true\n\n[roles.coder]\nmodel = 'x'\n"
    update = add_instructions(text, ROLES_REFERENCE, ("coder",), TODAY)
    assert update == InstructionsUpdate(f"{text}{CODER_LINE}\n", InstructionLines(added=("coder",)))


def test_a_table_setting_its_instructions_gets_no_line() -> None:
    text = "[roles.coder]\ninstructions = 'x.md'\n\n[roles.reviewer]\nmerge = false\n"
    assert with_instructions(text) == InstructionsUpdate(
        f"{text}{REVIEWER_LINE}\n", InstructionLines(added=("reviewer",))
    )


def test_a_table_with_its_instructions_commented_out_gets_no_line() -> None:
    text = "# [roles.coder]\n#instructions='x.md'\n\n[roles.reviewer]\nmerge = false\n"
    assert with_instructions(text).lines == InstructionLines(added=("reviewer",))


def test_tables_a_file_lacks_are_appended_with_the_line_under_one_dated_line() -> None:
    expected = (
        f"{LIMITS}\n{ADDED}\n\n{CODER_TABLE}{CODER_LINE}\n\n{REVIEWER_TABLE}{REVIEWER_LINE}\n"
    )
    assert with_instructions(LIMITS) == InstructionsUpdate(
        expected, InstructionLines(appended=ROLES)
    )


def test_a_line_and_a_table_can_be_added_at_once() -> None:
    text = "[roles.reviewer]\nmerge = false\n# The setup.\n[setup]\ncommand = ['make']\n"
    expected = (
        f"[roles.reviewer]\nmerge = false\n{REVIEWER_LINE}\n\n# The setup.\n"
        f"[setup]\ncommand = ['make']\n\n{ADDED}\n\n{CODER_TABLE}{CODER_LINE}\n"
    )
    assert with_instructions(text) == InstructionsUpdate(
        expected, InstructionLines(added=("reviewer",), appended=("coder",))
    )


def test_a_role_set_inline_gets_nothing_as_its_table_has_no_lines_of_its_own() -> None:
    text = "[roles]\ncoder = { model = 'x' }\n\n[roles.reviewer]\ninstructions = 'r.md'\n"
    assert with_instructions(text) == InstructionsUpdate(text, InstructionLines(inline=("coder",)))


def test_a_role_set_by_dotted_keys_gets_nothing_and_is_named_as_set_inline() -> None:
    update = with_instructions("roles.coder.model = 'x'\n")
    assert update.lines == InstructionLines(appended=("reviewer",), inline=("coder",))
    assert CODER_LINE not in update.text


def test_roles_under_commented_headers_are_those_commented_then_those_appended() -> None:
    lines = InstructionLines(added=("a",), commented=("b",), appended=("c",), inline=("d",))
    assert lines.under_commented_headers == ("b", "c")


def test_only_the_roles_named_get_lines() -> None:
    update = add_instructions(SET_TABLES, ROLES_REFERENCE, ("reviewer",), TODAY)
    assert update.lines == InstructionLines(added=("reviewer",))
    assert CODER_LINE not in update.text


def test_adding_instructions_to_a_file_that_isnt_toml_is_refused() -> None:
    with pytest.raises(ConfigError):
        with_instructions("[roles.coder\n")


@pytest.mark.parametrize("text", ["", LIMITS, SET_TABLES, ROLES_REFERENCE])
def test_adding_instructions_again_adds_nothing(text: str) -> None:
    once = with_instructions(text).text
    assert with_instructions(once) == InstructionsUpdate(once, InstructionLines())


LINT = VerificationStep("Lint", ("ruff", "check"))
TESTS = VerificationStep("Tests", ("pytest",))
SYNC = Setup(command=("uv", "sync"), watch=("uv.lock",))
CHECKS = ChecksProposal(steps=(LINT, TESTS), setup=SYNC)
LINT_ONLY = ChecksProposal(steps=(LINT,))

LINT_TABLE = '[[verification.steps]]\nname = "Lint"\ncommand = ["ruff", "check"]\n'
STEP_TABLES = f'{LINT_TABLE}\n[[verification.steps]]\nname = "Tests"\ncommand = ["pytest"]\n'
SYNC_TABLE = '[setup]\ncommand = ["uv", "sync"]\nwatch = ["uv.lock"]\n'
EXAMPLES = f"# The file's header.\n\n{STEPS}\n{SETUP_SECTION}\n{CODER_SECTION}"


def test_the_reference_gets_the_tables_in_place_of_its_examples_under_their_descriptions() -> None:
    expected = (
        f"# The file's header.\n\n# The steps.\n{STEP_TABLES}\n"
        f"# How the tree is set up.\n{SYNC_TABLE}\n{CODER_SECTION}"
    )
    assert reference_with_checks(EXAMPLES, CHECKS) == expected


def test_without_a_setup_the_references_example_of_one_stays() -> None:
    expected = (
        f"# The file's header.\n\n# The steps.\n{LINT_TABLE}\n{SETUP_SECTION}\n{CODER_SECTION}"
    )
    assert reference_with_checks(EXAMPLES, LINT_ONLY) == expected


def test_steps_from_the_firsts_header_to_the_lasts_last_setting_give_way_to_the_new_ones() -> None:
    old_steps = (
        '[[verification.steps]]\nname = "Old lint"\ncommand = ["flake8"]\n\n# The slow one.\n'
        '[[verification.steps]]\nname = "Old tests"\ncommand = [\n  "nose",\n]\n'
    )
    after = '# name = "Commented"\n\n# After the steps.\n[roles.coder]\nmodel = "opus"\n'
    text = f"# Ours.\nfix_attempts = 2\n\n# Quick ones first.\n{old_steps}{after}"
    expected = f"# Ours.\nfix_attempts = 2\n\n# Quick ones first.\n{LINT_TABLE}{after}"
    assert with_checks(text, LINT_ONLY) == expected


def test_the_setup_to_its_last_setting_gives_way_to_the_new_one() -> None:
    after = "\n# The roles.\n# [roles.coder]\n"
    text = f'{LINT_TABLE}\n# Setup.\n[setup]\n# Old.\ncommand = ["make"]\n{after}'
    expected = f"{STEP_TABLES}\n# Setup.\n{SYNC_TABLE}{after}"
    assert with_checks(text, CHECKS) == expected


def test_a_setup_watching_nothing_has_no_watch_line() -> None:
    checks = ChecksProposal(steps=(LINT,), setup=Setup(command=("make",)))
    assert with_checks("", checks) == f'{LINT_TABLE}\n[setup]\ncommand = ["make"]\n'


def test_without_a_new_setup_the_old_one_goes_with_the_blank_lines_above_it() -> None:
    text = f'{LINT_TABLE}\n\n[setup]\ncommand = ["make"]\n\n# The roles.\n'
    assert with_checks(text, LINT_ONLY) == f"{LINT_TABLE}\n# The roles.\n"


@pytest.mark.parametrize(
    ("text", "start"),
    [("", ""), (LIMITS, f"{LIMITS}\n"), (f"{LIMITS}\n", f"{LIMITS}\n")],
)
def test_steps_and_setup_a_file_lacks_are_appended_apart_from_it(text: str, start: str) -> None:
    assert with_checks(text, CHECKS) == f"{start}{STEP_TABLES}\n{SYNC_TABLE}"


def test_steps_split_by_another_table_are_replaced_where_the_first_were() -> None:
    later = '\n[[verification.steps]]\nname = "Old"\ncommand = ["nose"]\n'
    text = f'{LINT_TABLE}\n[roles.coder]\nmodel = "opus"\n{later}'
    assert with_checks(text, CHECKS) == (
        f'{STEP_TABLES}\n[roles.coder]\nmodel = "opus"\n\n{SYNC_TABLE}'
    )


def test_a_setup_set_inline_is_replaced_as_toml() -> None:
    text = 'fix_attempts = 2\nsetup = { command = ["make"] }\n'
    assert tomllib.loads(with_checks(text, LINT_ONLY)) == {
        "fix_attempts": 2,
        "verification": {"steps": [{"name": "Lint", "command": ["ruff", "check"]}]},
    }


def test_steps_set_inline_are_replaced_as_toml() -> None:
    text = '# Ours.\n[verification]\nsteps = [{ name = "Old", command = ["nose"] }]\n'
    checked = with_checks(text, LINT_ONLY)
    assert checked.startswith("# Ours.\n")
    assert tomllib.loads(checked) == {
        "verification": {"steps": [{"name": "Lint", "command": ["ruff", "check"]}]}
    }


def test_writing_checks_into_a_file_that_isnt_toml_is_refused() -> None:
    with pytest.raises(ConfigError):
        with_checks("[setup\n", CHECKS)


@pytest.mark.parametrize("checks", [CHECKS, LINT_ONLY])
def test_writing_the_same_checks_again_changes_nothing(checks: ChecksProposal) -> None:
    once = with_checks(reference_with_checks(EXAMPLES, CHECKS), checks)
    assert with_checks(once, checks) == once


MUTANTS = VerificationStep("Mutants", ("mutmut", "run"), set="mutation")
EVERY_10 = ChecksTrigger(on="commits", every=10, checks="mutation")
HOLDING = ChecksProposal(
    steps=(LINT, MUTANTS),
    sets={"mutation": VerificationSet(holds_merges=True)},
    triggers={"mutation-every-10": EVERY_10},
)
REPORTING = ChecksProposal(
    steps=(LINT, MUTANTS),
    sets={"mutation": VerificationSet(holds_merges=False)},
    triggers={"mutation-every-10": EVERY_10},
)
MUTANTS_TABLE = (
    '[[verification.steps]]\nname = "Mutants"\ncommand = ["mutmut", "run"]\nset = "mutation"\n'
)
SET_TABLE = "[verification.sets.mutation]\nholds_merges = true\n"
TRIGGER_TABLE = '[triggers.mutation-every-10]\non = "commits"\nchecks = "mutation"\nevery = 10\n'
SETS_SECTION = (
    "# The sets. For example:\n# [verification.sets.mutation]\n# Whether it holds.\n"
    "# holds_merges = true\n"
)


def test_a_steps_set_its_holding_sets_table_and_its_triggers_are_appended() -> None:
    assert with_checks("", HOLDING) == (
        f"{LINT_TABLE}\n{MUTANTS_TABLE}\n{SET_TABLE}\n{TRIGGER_TABLE}"
    )


def test_a_reporting_set_gets_no_table() -> None:
    assert with_checks("", REPORTING) == f"{LINT_TABLE}\n{MUTANTS_TABLE}\n{TRIGGER_TABLE}"


def test_a_trigger_writes_only_the_fields_it_sets() -> None:
    by_hand = ChecksTrigger(
        on="manual", checks="mutation", manual=True, label="Mutants", enabled=False
    )
    checks = ChecksProposal(steps=(LINT, MUTANTS), triggers={"mutants": by_hand})
    assert with_checks("", checks).endswith(
        '[triggers.mutants]\non = "manual"\nchecks = "mutation"\nmanual = true\n'
        'label = "Mutants"\nenabled = false\n'
    )


def test_proposed_sets_and_triggers_replace_theirs_and_leave_the_others() -> None:
    others = (
        '[[verification.steps]]\nname = "Stress"\ncommand = ["stress"]\nset = "stress"\n\n'
        "# Stress.\n[verification.sets.stress]\nholds_merges = true\n\n"
        '[triggers.nightly]\non = "manual"\nchecks = "stress"\n'
    )
    text = (
        f"{LINT_TABLE}\n# Ours.\n[verification.sets.mutation]\nholds_merges = false\n\n"
        f'[triggers.mutation-every-10]\non = "commits"\nchecks = "mutation"\nevery = 5\n\n'
        f"{others}"
    )
    assert with_checks(text, HOLDING) == (
        f"{LINT_TABLE}\n{MUTANTS_TABLE}\n# Ours.\n{SET_TABLE}\n{TRIGGER_TABLE}\n{others}"
    )


def test_a_set_proposed_as_reporting_loses_its_table() -> None:
    text = f"{LINT_TABLE}\n{MUTANTS_TABLE}\n{SET_TABLE}\n# The roles.\n"
    assert with_checks(text, REPORTING) == (
        f"{LINT_TABLE}\n{MUTANTS_TABLE}\n# The roles.\n\n{TRIGGER_TABLE}"
    )


def test_sets_and_triggers_set_inline_are_rewritten_as_toml_keeping_the_others() -> None:
    text = (
        '[verification]\nsteps = [{ name = "Stress", command = ["stress"], set = "stress" }]\n'
        "sets = { stress = { holds_merges = true } }\n\n"
        '[triggers]\nnightly = { on = "manual", checks = "stress" }\n'
    )
    assert tomllib.loads(with_checks(text, HOLDING)) == {
        "verification": {
            "steps": [
                {"name": "Lint", "command": ["ruff", "check"]},
                {"name": "Mutants", "command": ["mutmut", "run"], "set": "mutation"},
                {"name": "Stress", "command": ["stress"], "set": "stress"},
            ],
            "sets": {"stress": {"holds_merges": True}, "mutation": {"holds_merges": True}},
        },
        "triggers": {
            "nightly": {"on": "manual", "checks": "stress"},
            "mutation-every-10": {"on": "commits", "checks": "mutation", "every": 10},
        },
    }


def test_a_rewrite_drops_a_set_proposed_as_reporting() -> None:
    text = "[verification]\nsets = { mutation = { holds_merges = true } }\n"
    assert tomllib.loads(with_checks(text, REPORTING))["verification"] == {
        "steps": [
            {"name": "Lint", "command": ["ruff", "check"]},
            {"name": "Mutants", "command": ["mutmut", "run"], "set": "mutation"},
        ]
    }


def test_the_reference_gets_holding_sets_in_place_of_its_example_and_triggers_at_the_end() -> None:
    examples = f"# The file's header.\n\n{STEPS}\n{SETS_SECTION}\n{CODER_SECTION}"
    expected = (
        f"# The file's header.\n\n# The steps.\n{LINT_TABLE}\n{MUTANTS_TABLE}\n"
        f"# The sets.\n{SET_TABLE}\n{CODER_SECTION}\n{TRIGGER_TABLE}"
    )
    assert reference_with_checks(examples, HOLDING) == expected


def test_without_a_holding_set_the_references_example_of_one_stays() -> None:
    examples = f"{STEPS}\n{SETS_SECTION}"
    expected = f"# The steps.\n{LINT_TABLE}\n{MUTANTS_TABLE}\n{SETS_SECTION}\n{TRIGGER_TABLE}"
    assert reference_with_checks(examples, REPORTING) == expected


def test_a_reference_without_an_example_of_a_set_gets_its_table_at_the_end() -> None:
    expected = f"# The steps.\n{LINT_TABLE}\n{MUTANTS_TABLE}\n{SET_TABLE}\n{TRIGGER_TABLE}"
    assert reference_with_checks(STEPS, HOLDING) == expected


@pytest.mark.parametrize("checks", [HOLDING, REPORTING])
def test_writing_the_same_sets_and_triggers_again_changes_nothing(checks: ChecksProposal) -> None:
    once = with_checks(reference_with_checks(EXAMPLES, CHECKS), checks)
    assert with_checks(once, checks) == once


NIGHTLY_STEP = (
    '# Nightly, in a set.\n[[verification.steps]]\nname = "Mutation"\n# Its own comment.\n'
    'command = ["mutmut", "run"]\nset = "mutation"\n'
)
NIGHTLY_TABLES = f'{SET_TABLE}\n[triggers.nightly]\non = "commits"\nchecks = "mutation"\n'
OLD_LINT = '[[verification.steps]]\nname = "Old lint"\ncommand = ["flake8"]\n'


def loaded_checks(text: str) -> tuple[VerificationStep, ...]:
    """The task checks of the configuration a project file holding ``text`` makes."""
    package = Package(built_in_roles(read_built_in_roles(), INSTRUCTIONS), read_built_in_triggers())
    return load_layered(tomllib.loads(text), package).config.verification.task_checks


def test_steps_in_a_set_stay_as_written_after_the_new_task_checks() -> None:
    text = f"# Quick ones first.\n{OLD_LINT}\n{NIGHTLY_STEP}\n{NIGHTLY_TABLES}"
    checked = with_checks(text, CHECKS)
    assert checked == (
        f"# Quick ones first.\n{STEP_TABLES}\n{NIGHTLY_STEP}\n{NIGHTLY_TABLES}\n{SYNC_TABLE}"
    )
    assert loaded_checks(checked) == (LINT, TESTS)


def test_a_step_in_a_set_ahead_of_the_task_checks_goes_after_them_with_its_comments() -> None:
    text = f"# The file.\n\n{NIGHTLY_STEP}\n{OLD_LINT}\n{NIGHTLY_TABLES}"
    assert with_checks(text, LINT_ONLY) == (
        f"# The file.\n\n{LINT_TABLE}\n{NIGHTLY_STEP}\n{NIGHTLY_TABLES}"
    )


def test_a_step_in_a_set_split_from_the_others_stays_where_it_was() -> None:
    later = f'\n[roles.coder]\nmodel = "opus"\n\n{OLD_LINT}\n{NIGHTLY_STEP}'
    text = f"{OLD_LINT}{later}\n{NIGHTLY_TABLES}"
    assert with_checks(text, LINT_ONLY) == (
        f'{LINT_TABLE}\n[roles.coder]\nmodel = "opus"\n\n{NIGHTLY_STEP}\n{NIGHTLY_TABLES}'
    )


def test_a_proposed_sets_steps_replace_that_sets_and_leave_the_other_sets() -> None:
    stress = '[[verification.steps]]\nname = "Stress"\ncommand = ["stress"]\nset = "stress"\n'
    text = f"{OLD_LINT}\n{NIGHTLY_STEP}\n{stress}\n{NIGHTLY_TABLES}"
    checks = ChecksProposal(
        steps=(LINT, MUTANTS), sets={"mutation": VerificationSet(holds_merges=True)}
    )
    assert with_checks(text, checks) == (
        f"{LINT_TABLE}\n{MUTANTS_TABLE}\n{stress}\n{NIGHTLY_TABLES}"
    )


UNLISTED = ChecksProposal(steps=(LINT, MUTANTS))
NIGHTLY_TRIGGER = '[triggers.nightly]\non = "commits"\nchecks = "mutation"\n'


def test_a_set_left_out_of_sets_is_written_as_reporting_without_the_files_holding_table() -> None:
    text = f"{OLD_LINT}\n{NIGHTLY_STEP}\n{NIGHTLY_TABLES}"
    assert with_checks(text, UNLISTED) == f"{LINT_TABLE}\n{MUTANTS_TABLE}\n{NIGHTLY_TRIGGER}"


def test_a_rewrite_drops_the_holding_table_of_a_set_left_out_of_sets() -> None:
    text = f'setup = {{ command = ["make"] }}\n\n{OLD_LINT}\n{NIGHTLY_STEP}\n{NIGHTLY_TABLES}'
    assert "sets" not in tomllib.loads(with_checks(text, UNLISTED))["verification"]


STALE = "[verification.sets.stale]\nholds_merges = true\n"


def test_a_sets_table_no_written_step_is_in_goes_so_the_file_loads() -> None:
    text = f"{OLD_LINT}\n{NIGHTLY_STEP}\n{STALE}\n{NIGHTLY_TABLES}"
    checked = with_checks(text, LINT_ONLY)
    assert checked == f"{LINT_TABLE}\n{NIGHTLY_STEP}\n{NIGHTLY_TABLES}"
    assert loaded_checks(checked) == (LINT,)


def test_a_rewrite_drops_a_sets_table_no_written_step_is_in_so_the_file_loads() -> None:
    text = (
        f'setup = {{ command = ["make"] }}\n\n{OLD_LINT}\n{NIGHTLY_STEP}\n'
        f"[verification.sets.stale]\nholds_merges = true\n\n{NIGHTLY_TABLES}"
    )
    checked = with_checks(text, LINT_ONLY)
    assert tomllib.loads(checked)["verification"]["sets"] == {"mutation": {"holds_merges": True}}
    assert loaded_checks(checked) == (LINT,)


def test_steps_in_a_set_set_inline_are_kept_when_rewritten_as_toml() -> None:
    text = (
        "[verification]\nsteps = [\n"
        '  { name = "Old", command = ["nose"] },\n'
        '  { name = "Mutation", command = ["mutmut", "run"], set = "mutation" },\n]\n'
        "sets = { mutation = { holds_merges = true } }\n\n"
        '[triggers.nightly]\non = "commits"\nchecks = "mutation"\n'
    )
    checked = with_checks(text, LINT_ONLY)
    assert tomllib.loads(checked)["verification"] == {
        "steps": [
            {"name": "Lint", "command": ["ruff", "check"]},
            {"name": "Mutation", "command": ["mutmut", "run"], "set": "mutation"},
        ],
        "sets": {"mutation": {"holds_merges": True}},
    }
    assert loaded_checks(checked) == (LINT,)


def test_a_rewrite_keeps_a_step_in_a_set_written_as_its_own_table() -> None:
    text = f'setup = {{ command = ["make"] }}\n\n{OLD_LINT}\n{NIGHTLY_STEP}\n{NIGHTLY_TABLES}'
    checked = with_checks(text, LINT_ONLY)
    assert "# Its own comment.\n" in checked
    assert tomllib.loads(checked)["verification"]["steps"] == [
        {"name": "Lint", "command": ["ruff", "check"]},
        {"name": "Mutation", "command": ["mutmut", "run"], "set": "mutation"},
    ]
    assert loaded_checks(checked) == (LINT,)


def test_a_key_missing_from_the_table_before_the_heading_goes_above_it() -> None:
    lacking = CURRENT.removesuffix("merge = true\n")
    text = f"{lacking}\n{HEADING}\n\n{LATER_SECTIONS}"
    update = later_updated(text)
    assert update.additions == MERGE_ADDED
    above, below = update.text.split(HEADING)
    assert "merge" in above
    assert "merge" not in below


def test_a_runtime_only_under_the_heading_isnt_appended_again() -> None:
    text = f"{CURRENT}\n{HEADING}\n\n{LATER_SECTIONS}"
    assert later_updated(text) == ConfigUpdate(text, Additions())


def test_keys_added_after_the_heading_was_appended_go_above_the_dated_line() -> None:
    lacking = CURRENT.removesuffix("merge = true\n")
    reference = f"{REFERENCE.replace(MERGE_LINES, '')}\n{HEADING}\n\n{RUNTIME_LINES}"
    once = update_text(lacking, reference, TODAY, later_heading=LATER).text
    assert once == f"{lacking}\n{ADDED}\n\n{HEADING}\n\n{RUNTIME_LINES}"
    again = update_text(once, f"{REFERENCE}\n{HEADING}\n\n{RUNTIME_LINES}", TODAY, LATER)
    assert again.text.startswith(f"{lacking}{MERGE_LINES}\n{ADDED}\n")
    roles = add_instructions(once, ROLES_REFERENCE, ("coder",), TODAY, later_heading=LATER)
    assert roles.text.startswith(f"{lacking}{CODER_LINE}\n\n{ADDED}\n")


def test_without_the_heading_keys_go_after_the_lines_below_the_table() -> None:
    text = f"[roles.coder]\nmodel = 'x'\n\n{HEADING}\n# runtime = 'claude'\n"
    roles = add_instructions(text, ROLES_REFERENCE, ("coder",), TODAY)
    assert roles.text == f"{text}{CODER_LINE}\n"


def _following_the_advice(text: str, header: str | None) -> dict[str, object]:
    """``text`` read as TOML with :data:`CODER_LINE` uncommented, and ``header`` with it."""
    if header is not None:
        text = text.replace(f"# {header}\n", f"{header}\n")
    return tomllib.loads(text.replace(CODER_LINE, CODER_LINE.removeprefix("# ")))


@pytest.mark.parametrize(
    "text", ["[roles]\ncoder.model = 'opus'\n\n", "[roles]\ncoder = { model = 'opus' }\n\n"]
)
def test_a_role_set_inline_above_a_commented_header_gets_nothing(text: str) -> None:
    update = with_instructions(text + CODER_TABLE)
    assert update.lines == InstructionLines(appended=("reviewer",), inline=("coder",))
    assert CODER_LINE not in update.text


def test_a_set_header_followed_by_a_commented_duplicate_gets_the_line_in_the_set_one() -> None:
    text = f"[roles.coder]\nmodel = 'opus'\n\n{CODER_TABLE}"
    update = with_instructions(text)
    assert update.lines == InstructionLines(added=("coder",), appended=("reviewer",))
    assert update.text.startswith(f"[roles.coder]\nmodel = 'opus'\n{CODER_LINE}\n\n{CODER_TABLE}")
    assert _following_the_advice(update.text, None)["roles"] == {
        "coder": {"model": "opus", "instructions": "roles/coder.md"}
    }


def test_a_table_only_under_a_commented_header_follows_its_advice_to_a_valid_config() -> None:
    update = with_instructions(CODER_TABLE)
    assert update.lines.commented == ("coder",)
    roles = _following_the_advice(update.text, "[roles.coder]")["roles"]
    assert roles == {"coder": {"instructions": "roles/coder.md"}}
