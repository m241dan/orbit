"""The protocol document: its code tables list every refusal and error code the protocol has."""

import re
from pathlib import Path

from orbit.protocol import (
    AGENT_FAILED,
    BUSY,
    CHECKS_EXHAUSTED,
    ERROR_CODES,
    GIT_FAILED,
    INCOMPATIBLE,
    INTERNAL,
    INVALID,
    LOCKED,
    NOT_CONFIGURED,
    REFUSAL_CODES,
    SAVE_FAILED,
    TIMED_OUT,
    UNKNOWN,
    WRONG_SESSION,
)

PROTOCOL_DOC = Path(__file__).parent.parent / "docs" / "protocol.md"
CODE_ROW = re.compile(r"^\| `(\w+)` \|", re.MULTILINE)


def code_tables(markdown: str) -> list[set[str]]:
    """The first-column codes of each table in ``markdown``'s ``## Codes`` section, in order."""
    section = markdown.split("\n## Codes\n", 1)[1].split("\n## ", 1)[0]
    blocks = (set(CODE_ROW.findall(block)) for block in section.split("\n\n"))
    return [codes for codes in blocks if codes]


def code_table_problems(markdown: str) -> list[str]:
    """What the refusals and errors tables in ``markdown`` are missing or have extra."""
    tables = code_tables(markdown)
    if len(tables) != 2:
        return [f"expected a refusals and an errors table, found {len(tables)} tables"]
    expected = {"refusals": set(REFUSAL_CODES), "errors": set(ERROR_CODES)}
    problems: list[str] = []
    for (name, want), have in zip(expected.items(), tables, strict=True):
        if missing := want - have:
            problems.append(f"the {name} table is missing {', '.join(sorted(missing))}")
        if extra := have - want:
            problems.append(f"the {name} table has extra {', '.join(sorted(extra))}")
    return problems


def codes_doc(refusals: list[str], errors: list[str]) -> str:
    """A small protocol doc whose prose names every code and whose tables list the given ones."""
    prose = " ".join(f"`{code}`" for code in REFUSAL_CODES + ERROR_CODES)
    refusal_rows = "".join(f"| `{code}` | When. |\n" for code in refusals)
    error_rows = "".join(f"| `{code}` | When. |\n" for code in errors)
    return (
        f"# Protocol\n\n{prose}\n\n## Codes\n\nRefusals:\n\n| Code | When |\n| --- | --- |\n"
        f"{refusal_rows}\nErrors:\n\n| Code | When |\n| --- | --- |\n{error_rows}\n## Later\n\n"
        "| `stray` | Not in the codes section. |\n"
    )


def test_refusal_codes_are_the_seven_refusals() -> None:
    assert set(REFUSAL_CODES) == {
        NOT_CONFIGURED,
        UNKNOWN,
        BUSY,
        INVALID,
        LOCKED,
        WRONG_SESSION,
        INCOMPATIBLE,
    }
    assert len(REFUSAL_CODES) == 7


def test_error_codes_are_the_six_errors() -> None:
    assert set(ERROR_CODES) == {
        AGENT_FAILED,
        TIMED_OUT,
        GIT_FAILED,
        SAVE_FAILED,
        CHECKS_EXHAUSTED,
        INTERNAL,
    }
    assert len(ERROR_CODES) == 6


def test_refusal_and_error_codes_do_not_overlap() -> None:
    assert not set(REFUSAL_CODES) & set(ERROR_CODES)


def test_code_tables_with_every_code_have_no_problems() -> None:
    assert code_table_problems(codes_doc(list(REFUSAL_CODES), list(ERROR_CODES))) == []


def test_a_refusal_row_missing_is_a_problem_even_when_the_prose_names_it() -> None:
    refusals = [code for code in REFUSAL_CODES if code != INVALID]
    assert code_table_problems(codes_doc(refusals, list(ERROR_CODES))) == [
        f"the refusals table is missing {INVALID}"
    ]


def test_a_row_for_a_code_that_does_not_exist_is_a_problem() -> None:
    errors = [*ERROR_CODES, "stale"]
    assert code_table_problems(codes_doc(list(REFUSAL_CODES), errors)) == [
        "the errors table has extra stale"
    ]


def test_an_error_code_in_the_refusals_table_is_a_problem() -> None:
    refusals = [*REFUSAL_CODES, INTERNAL]
    errors = [code for code in ERROR_CODES if code != INTERNAL]
    assert code_table_problems(codes_doc(refusals, errors)) == [
        f"the refusals table has extra {INTERNAL}",
        f"the errors table is missing {INTERNAL}",
    ]


def test_a_codes_section_without_two_tables_is_a_problem() -> None:
    assert code_table_problems(codes_doc(list(REFUSAL_CODES), [])) == [
        "expected a refusals and an errors table, found 1 tables"
    ]


def test_protocol_doc_tables_list_every_code() -> None:
    problems = code_table_problems(PROTOCOL_DOC.read_text())
    assert not problems, f"docs/protocol.md's code tables are wrong: {'; '.join(problems)}"


def test_protocol_doc_says_a_refusal_may_carry_internal() -> None:
    section = PROTOCOL_DOC.read_text().split("\n## Command outcomes\n", 1)[1].split("\n## ", 1)[0]
    refused = re.search(r"`CommandRefused` says why in `code`,[^.]*\.", section)
    assert refused is not None
    assert f"`{INTERNAL}`" in refused.group()
