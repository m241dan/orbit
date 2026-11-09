"""The checks the language skills suggest: each language skill points to its ``checks.md``, a
Suggested checks table of what each check catches, its tool and command, and where it fits, and
states no check as one that runs."""

import re

import pytest

from orbit.agent.domain.triggers import EVENTS, SET_FINISHED, template_variables
from orbit.agent.external.skill_files import SKILL_DIRECTORY
from orbit.protocol import BUILT_IN_ROLE_NAMES

OPENING = (
    "Suggestions for the project's verification: the configurator proposes these, and the"
    " project's .orbit/config.toml decides what runs."
)
HEADER = "| Check | Catches | Tool | Command | Where it fits |"
FITS = re.compile(
    r"^task checks|^set `[a-z]+`, (every \d+ commits|nightly|by hand),"
    r" (holds merges|reports|reports to `[a-z-]+-reviewer`)$"
)
RUNS = re.compile(r"\b(lizard|pytest|mutmut|hypothesis|mypy|gcovr|mull)\b\s+(runs|holds)", re.I)
LANGUAGES = ("python", "cpp")


def skill_text(skill: str, path: str) -> str:
    """The text of the shipped skill ``skill``'s file at ``path``."""
    return (SKILL_DIRECTORY / skill / path).read_text(encoding="utf-8")


def table_rows(text: str) -> list[list[str]]:
    """The cells of each row of the table in ``text`` below its header and divider."""
    rows = [line for line in text.splitlines() if line.startswith("|")][2:]
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows]


def suggested(skill: str) -> dict[str, list[str]]:
    """The rows of the shipped skill ``skill``'s Suggested checks table, by check."""
    return {row[0]: row for row in table_rows(skill_text(skill, "checks.md"))}


@pytest.mark.parametrize("skill", LANGUAGES)
def test_a_language_skill_points_to_its_suggested_checks(skill: str) -> None:
    body = " ".join(skill_text(skill, "SKILL.md").split())
    assert f"## Suggested checks {OPENING}" in body
    assert f"Read `{skill}`'s `checks.md`." in body


@pytest.mark.parametrize("skill", LANGUAGES)
def test_a_language_skills_checks_open_saying_the_project_decides_what_runs(skill: str) -> None:
    text = " ".join(skill_text(skill, "checks.md").split())
    assert text.startswith(f"# Suggested checks {OPENING} {HEADER} ")


@pytest.mark.parametrize("skill", LANGUAGES)
def test_each_suggested_check_says_where_it_fits(skill: str) -> None:
    for check, row in suggested(skill).items():
        assert len(row) == 5, check
        assert FITS.search(row[4]), check


@pytest.mark.parametrize("skill", LANGUAGES)
def test_a_language_skill_states_no_check_as_one_that_runs(skill: str) -> None:
    for path in ("SKILL.md", "checks.md"):
        assert not RUNS.search(skill_text(skill, path)), path


def test_python_suggests_the_quick_checks_for_every_task_and_mutation_testing_as_a_set() -> None:
    rows = suggested("python")
    assert [rows[check][4] for check in rows] == [
        "task checks",
        "task checks",
        "task checks",
        "task checks",
        "task checks, with the test that counts parameters",
        "task checks",
        "set `mutation`, every 10 commits, reports to `tests-reviewer`",
        "task checks, inside the tests",
        "set `bench`, every 10 commits, reports to `scaling-reviewer`",
    ]
    assert "--cov-fail-under=100" in rows["Tests with 100% branch coverage"][3]
    assert "-C 6 -L 100" in rows["Function limits"][3]


def test_cpp_suggests_tsan_as_a_set_that_holds_merges() -> None:
    rows = suggested("cpp")
    assert rows["ThreadSanitizer"][4] == "set `tsan`, every 10 commits, holds merges"
    assert (
        rows["Mutation testing"][4]
        == "set `mutation`, every 10 commits, reports to `tests-reviewer`"
    )
    assert rows["Benchmarks"][4] == "set `bench`, every 10 commits, reports to `scaling-reviewer`"
    for check in ("Formatting", "clang-tidy", "Function limits", "Layers", "Coverage"):
        assert rows[check][4] == "task checks", check
    assert rows["AddressSanitizer and UBSan"][4] == "task checks"


def test_cpp_suggests_the_include_checker_it_ships_copied_to_tools() -> None:
    tool, command = suggested("cpp")["Layers"][2:4]
    assert command == "`python3 tools/check_includes.py`"
    assert (
        "`scripts/check_includes.py` (read with `read_skill`) to `tools/check_includes.py`" in tool
    )


@pytest.mark.parametrize("skill", LANGUAGES)
def test_each_reporting_set_names_a_shipped_lens_role(skill: str) -> None:
    lenses = {
        row[4].rsplit("`", 2)[1] for row in suggested(skill).values() if "reports to" in row[4]
    }
    assert lenses == {"tests-reviewer", "scaling-reviewer"}
    assert lenses <= set(BUILT_IN_ROLE_NAMES)


@pytest.mark.parametrize("skill", LANGUAGES)
def test_each_lens_report_has_a_sample_prompt_using_only_set_finisheds_variables(
    skill: str,
) -> None:
    text = skill_text(skill, "checks.md")
    prompts = re.findall(r"^```text\n(.*?)^```$", text, re.M | re.S)
    assert len(prompts) == 2
    for prompt in prompts:
        assert set(template_variables(prompt)) <= set(EVENTS[SET_FINISHED].variables)
        assert "{output}" in prompt


def test_python_wraps_mutmut_so_surviving_mutants_fail_the_step() -> None:
    text = skill_text("python", "checks.md")
    assert 'test -z "$survivors"' in text
    assert "--benchmark-compare-fail=mean:10%" in suggested("python")["Benchmarks"][3]


def test_cpp_wraps_compare_py_so_a_slowdown_fails_the_step() -> None:
    text = skill_text("cpp", "checks.md")
    assert "sys.exit(1 if slow else 0)" in text
    assert "exits non-zero when a mutant survives" in suggested("cpp")["Mutation testing"][3]


def test_cpp_sizes_each_builds_parallelism_without_a_shell() -> None:
    text = " ".join(skill_text("cpp", "checks.md").split())
    assert "`run-clang-tidy` take `-j N`" in text
    assert "`env CMAKE_BUILD_PARALLEL_LEVEL=N cmake --workflow --preset asan`" in text
