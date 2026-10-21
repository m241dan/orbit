"""The protocol document: its code tables list every refusal and error code the protocol has,
and its Transport section states the transport's close codes, statuses, reason and header."""

import re
from pathlib import Path

from orbit.protocol import (
    AGENT_FAILED,
    BUSY,
    CHECKS_EXHAUSTED,
    ERROR_CODES,
    FORBIDDEN,
    GIT_FAILED,
    GOING_AWAY,
    INCOMPATIBLE,
    INTERNAL,
    INVALID,
    LOCKED,
    MESSAGE_TOO_BIG,
    NORMAL,
    NOT_ACTIVE,
    NOT_CONFIGURED,
    POLICY_VIOLATION,
    REFUSAL_CODES,
    SAVE_FAILED,
    SHUTTING_DOWN,
    STORAGE_FAILED,
    TIMED_OUT,
    UNAUTHORIZED,
    UNAVAILABLE,
    UNKNOWN,
    WRONG_SESSION,
    bearer_header,
)

PROTOCOL_DOC = Path(__file__).parent.parent / "docs" / "protocol.md"
CODE_ROW = re.compile(r"^\| `(\w+)` \|", re.MULTILINE)
TRANSPORT_PHRASES = (
    *(f"code {code}" for code in (NORMAL, GOING_AWAY, POLICY_VIOLATION, MESSAGE_TOO_BIG)),
    *(f"HTTP {status}" for status in (UNAUTHORIZED, FORBIDDEN, UNAVAILABLE)),
    f"the reason `{SHUTTING_DOWN}`",
    *(f"`{name}: {value}`" for name, value in bearer_header("<token>").items()),
)
"""What the Transport section must say for each of the transport's constants."""


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


def test_refusal_codes_are_the_nine_refusals() -> None:
    assert set(REFUSAL_CODES) == {
        NOT_CONFIGURED,
        UNKNOWN,
        BUSY,
        INVALID,
        LOCKED,
        WRONG_SESSION,
        NOT_ACTIVE,
        INCOMPATIBLE,
        STORAGE_FAILED,
    }
    assert len(REFUSAL_CODES) == 9


def test_error_codes_are_the_seven_errors() -> None:
    assert set(ERROR_CODES) == {
        AGENT_FAILED,
        TIMED_OUT,
        GIT_FAILED,
        SAVE_FAILED,
        STORAGE_FAILED,
        CHECKS_EXHAUSTED,
        INTERNAL,
    }
    assert len(ERROR_CODES) == 7


def test_refusal_and_error_codes_share_only_storage_failed() -> None:
    assert set(REFUSAL_CODES) & set(ERROR_CODES) == {STORAGE_FAILED}


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


def transport_problems(markdown: str) -> list[str]:
    """The transport phrases ``markdown``'s ``## Transport`` section doesn't say."""
    section = markdown.split("\n## Transport\n", 1)[1].split("\n## ", 1)[0]
    flat = " ".join(section.split())
    return [f"it doesn't say {phrase}" for phrase in TRANSPORT_PHRASES if phrase not in flat]


def transport_doc() -> str:
    """A small protocol doc whose Transport section says every transport phrase."""
    return f"# Protocol\n\n## Transport\n\n{'. '.join(TRANSPORT_PHRASES)}.\n\n## Later\n"


def test_a_transport_section_saying_every_phrase_has_no_problems() -> None:
    assert transport_problems(transport_doc()) == []


def test_a_transport_phrase_only_outside_the_section_is_a_problem() -> None:
    doc = transport_doc().replace(f"code {GOING_AWAY}", "code 4000") + f"code {GOING_AWAY}\n"
    assert transport_problems(doc) == [f"it doesn't say code {GOING_AWAY}"]


def test_protocol_doc_transport_states_the_transport_constants() -> None:
    problems = transport_problems(PROTOCOL_DOC.read_text())
    assert not problems, f"docs/protocol.md's Transport section is wrong: {'; '.join(problems)}"
