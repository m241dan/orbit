"""The protocol document: it names every refusal and error code the protocol has."""

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


def test_protocol_doc_names_every_code() -> None:
    text = PROTOCOL_DOC.read_text()
    missing = [code for code in REFUSAL_CODES + ERROR_CODES if f"`{code}`" not in text]
    assert not missing, f"docs/protocol.md doesn't name these codes: {', '.join(missing)}"
