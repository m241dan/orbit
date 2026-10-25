"""What ``orbit init --update`` adds to a project file from the reference config: the keys a
table it has doesn't mention, after the table's last line, and the tables it lacks, at the end
under a dated line, never changing, moving or removing a line it had; and the lines ``orbit init
--roles`` adds, pointing each built-in role at its copy of its instructions."""

from datetime import date

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.config_update import (
    Additions,
    ConfigUpdate,
    InstructionLines,
    InstructionsUpdate,
    add_instructions,
    instructions_setting,
    update_text,
)

TODAY = date(2026, 10, 25)

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
        expected, InstructionLines(added=ROLES)
    )


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
    assert with_instructions(text) == InstructionsUpdate(text, InstructionLines())


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
