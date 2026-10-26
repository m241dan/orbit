"""Kiro's stream-json output, translated into orbit's events: from the captured fixtures, and from
lines made up for what they don't show."""

import json
from pathlib import Path
from typing import Any

import pytest

from orbit.agent.external.kiro_stream import KiroTranslation, translate_kiro
from orbit.protocol import (
    AGENT_FAILED,
    AssistantMessage,
    ErrorOccurred,
    Event,
    ToolCallFinished,
    ToolCallStarted,
    TurnEndReason,
    TurnFinished,
    TurnStarted,
    Usage,
)

KIRO = Path(__file__).parent / "fixtures" / "kiro"
SESSION_ID = "00000000-0000-0000-0000-000000000001"


def fixture(name: str) -> KiroTranslation:
    """The translation of the Kiro fixture ``name``, as turn ``t``."""
    return translate_kiro("t", (KIRO / name).read_text().splitlines())


def line(kind: str, **data: Any) -> str:
    """A line of Kiro's stream: an event of type ``kind`` holding ``data``."""
    return json.dumps({"type": kind, "data": data})


def update(kind: str, **fields: Any) -> str:
    """A line of Kiro's stream: a session update of kind ``kind`` with ``fields``."""
    return line("sessionUpdate", update={"sessionUpdate": kind, **fields})


def chunk(kind: str, text: str) -> str:
    """A line of Kiro's stream: a ``kind`` chunk of ``text``."""
    return update(kind, content={"type": "text", "text": text})


def tool_call(**fields: Any) -> str:
    """A line of Kiro's stream: the start of call ``c`` to Kiro's ``read`` tool, with ``fields``."""
    fields = {
        "toolCallId": "c",
        "title": "Reading",
        "_meta": {"kiro": {"toolName": "read"}},
        **fields,
    }
    return update("tool_call", **fields)


def finished(**fields: Any) -> str:
    """A line of Kiro's stream: the end of call ``c``, with ``fields``."""
    return update("tool_call_update", toolCallId="c", **fields)


def translated(*lines: str) -> list[Event]:
    """The events of a stream of ``lines``, as turn ``t``."""
    return list(translate_kiro("t", lines))


def turn_finished(events: list[Event]) -> TurnFinished:
    """The TurnFinished that ends ``events``."""
    assert isinstance(events[-1], TurnFinished)
    return events[-1]


def usage(events: list[Event]) -> Usage:
    """What the turn ``events`` end cost."""
    cost = turn_finished(events).usage
    assert cost is not None
    return cost


# The fixtures


def test_a_plain_answer_is_one_message_in_a_completed_turn() -> None:
    events = list(fixture("plain.jsonl"))
    assert events[:2] == [TurnStarted(turn_id="t"), AssistantMessage(turn_id="t", text="ok")]
    assert turn_finished(events).reason == "completed"
    assert usage(events).credits == pytest.approx(0.188, abs=1e-3)
    assert usage(events).model_copy(update={"credits": 0}) == Usage(duration_ms=5711)
    assert len(events) == 3


def test_the_stream_reports_its_session_reasoning_and_stop_reason() -> None:
    translation = fixture("plain.jsonl")
    list(translation)
    assert translation.session_id == SESSION_ID
    assert translation.reasoning_unavailable
    assert translation.stop_reason == "end_turn"


def test_thoughts_and_answers_are_joined_around_tool_calls() -> None:
    events = list(fixture("tools.jsonl"))
    kinds = [type(event) for event in events]
    assert kinds == [
        TurnStarted,
        AssistantMessage,
        ToolCallStarted,
        ToolCallFinished,
        ToolCallStarted,
        ToolCallFinished,
        AssistantMessage,
        AssistantMessage,
        TurnFinished,
    ]
    assert events[1] == AssistantMessage(
        turn_id="t",
        text="The user wants me to:\n1. List the files in the current directory"
        " (/home/user/orbit)\n2. Tell them the first line of README.md\n\nLet me do both in"
        " parallel.",
    )
    answer = events[7]
    assert isinstance(answer, AssistantMessage)
    assert answer.text.startswith("Here are the files in `/home/user/orbit`:")
    assert "`.pytest_cache/`, `.ruff_cache/`" in answer.text
    assert answer.text.endswith("The first line of README.md is:\n\n```\n# orbit\n```")


def test_kiros_tool_calls_name_its_tool_and_give_its_output() -> None:
    events = list(fixture("tools.jsonl"))
    assert events[2] == ToolCallStarted(
        turn_id="t",
        call_id="call_8782701d562318b0",
        name="read",
        arguments={"operations": [{"mode": "Directory", "path": "/home/user/orbit", "depth": 0}]},
    )
    listing = events[3]
    assert isinstance(listing, ToolCallFinished)
    assert listing.call_id == "call_8782701d562318b0"
    assert listing.output.startswith("User id: 1001\n-rw-r--r-- 1 1001 1001 7905")
    assert not listing.is_error
    assert events[5] == ToolCallFinished(
        turn_id="t", call_id="call_97eea18082e31953", output="# orbit"
    )


def test_a_turns_credits_are_summed_over_its_requests() -> None:
    events = list(fixture("tools.jsonl"))
    total = 0.2113124008291874 + 0.2052497213930348 + 0.21860800381426204
    assert usage(events).credits == pytest.approx(total)
    assert usage(events).duration_ms == 18048
    assert usage(events).tokens == 0
    assert usage(events).cost_usd == 0


def test_orbits_tools_are_called_through_its_mcp_server() -> None:
    events = list(fixture("orbit-tools.jsonl"))
    calls = [event for event in events if isinstance(event, ToolCallStarted)]
    assert [call.name for call in calls] == ["get_task", "report_progress", "complete_task"]
    assert [call.arguments for call in calls] == [{}, {"note": "capturing"}, {"summary": "done"}]


def test_orbits_tool_results_are_given_as_their_text() -> None:
    events = list(fixture("orbit-tools.jsonl"))
    finishes = [event for event in events if isinstance(event, ToolCallFinished)]
    assert [event.output for event in finishes][1:] == ["Noted.", "Verified: the work is done."]
    assert finishes[0].output.startswith("Capture this run for orbit's fixtures")
    assert not any(event.is_error for event in finishes)


def test_a_tool_output_without_text_is_given_as_json() -> None:
    raw_output = {"items": [{"Json": {"isError": False}}, {"Json": {}}]}
    events = translated(finished(status="completed", rawOutput=raw_output))
    assert events == [ToolCallFinished(turn_id="t", call_id="c", output=json.dumps(raw_output))]


def test_a_completed_mcp_result_marked_as_an_error_finishes_in_error() -> None:
    result = {"content": [{"type": "text", "text": "Refused."}], "isError": True}
    events = translated(finished(status="completed", rawOutput={"items": [{"Json": result}]}))
    assert events == [ToolCallFinished(turn_id="t", call_id="c", output="Refused.", is_error=True)]


def test_text_items_and_mcp_result_texts_are_joined_in_order() -> None:
    content = [{"type": "text", "text": "b"}, {"type": "image"}, {"type": "text", "text": "c"}]
    raw_output = {"items": [{"Text": "a"}, {"Json": {"content": content}}, {"Text": "d"}]}
    events = translated(finished(status="completed", rawOutput=raw_output))
    assert events == [ToolCallFinished(turn_id="t", call_id="c", output="a\nb\nc\nd")]


def test_a_bad_model_fails_the_turn_with_kiros_message() -> None:
    events = list(fixture("bad-model.jsonl"))
    error = events[-2]
    assert isinstance(error, ErrorOccurred)
    assert error.code == AGENT_FAILED
    assert error.message.startswith("Internal error (code -32603): Encountered an error in the")
    assert "The model 'no-such-model' is not available." in error.message
    assert events[-1] == TurnFinished(turn_id="t", reason="error", usage=Usage(duration_ms=1812))


def test_an_outage_fails_the_turn_without_reporting_reasoning_unavailable() -> None:
    translation = fixture("outage.jsonl")
    events = list(translation)
    assert events == [
        TurnStarted(turn_id="t"),
        ErrorOccurred(
            turn_id="t",
            code=AGENT_FAILED,
            message="Internal error (code -32603): Encountered an error in the response stream:"
            " An unknown error occurred: dispatch failure",
        ),
        TurnFinished(turn_id="t", reason="error", usage=Usage()),
    ]
    assert translation.session_id == SESSION_ID
    assert not translation.reasoning_unavailable
    assert translation.stop_reason is None
    assert translation.error_stage == "prompt"


# Lines the fixtures don't show


def test_the_stage_a_run_failed_at_is_noted_and_none_before_it_fails() -> None:
    translation = KiroTranslation("t")
    translation.feed(line("runStarted"))
    assert translation.error_stage is None
    translation.feed(line("runError", stage="init", message="No such session"))
    assert translation.error_stage == "init"
    assert translation.session_id is None


def test_a_run_error_without_a_stage_notes_none() -> None:
    translation = KiroTranslation("t")
    translation.feed(line("runError", message="Broken"))
    assert translation.error_stage is None


@pytest.mark.parametrize(
    ("stop_reason", "reason"),
    [
        ("end_turn", "completed"),
        ("cancelled", "cancelled"),
        ("max_tokens", "max_steps"),
        ("max_turn_requests", "max_steps"),
        ("something_new", "completed"),
    ],
)
def test_kiros_stop_reason_says_how_the_turn_ended(stop_reason: str, reason: TurnEndReason) -> None:
    translation = translate_kiro("t", [line("runFinished", stopReason=stop_reason)])
    assert list(translation) == [TurnFinished(turn_id="t", reason=reason, usage=Usage())]
    assert translation.stop_reason == stop_reason


def test_a_run_finished_without_a_stop_reason_completes_the_turn() -> None:
    translation = translate_kiro("t", [line("runFinished", status="success")])
    assert list(translation) == [TurnFinished(turn_id="t", reason="completed", usage=Usage())]
    assert translation.stop_reason is None


@pytest.mark.parametrize(
    "skipped",
    [
        "",
        "not json",
        "[1, 2]",
        line("somethingNew", sessionId="other"),
        json.dumps({"no": "type"}),
        update("plan", entries=[]),
        finished(status="in_progress"),
    ],
)
def test_lines_that_arent_json_or_of_a_known_type_are_skipped(skipped: str) -> None:
    assert translated(
        chunk("agent_message_chunk", "a"), skipped, chunk("agent_message_chunk", "b")
    ) == [AssistantMessage(turn_id="t", text="ab")]


def test_a_message_is_held_until_another_event_arrives() -> None:
    translation = KiroTranslation("t")
    assert translation.feed(chunk("agent_message_chunk", "Look")) == []
    assert translation.feed(chunk("agent_message_chunk", "ing.")) == []
    assert translation.feed(tool_call()) == [
        AssistantMessage(turn_id="t", text="Looking."),
        ToolCallStarted(turn_id="t", call_id="c", name="read", arguments={}),
    ]
    assert translation.end() == []


def test_thoughts_and_answers_are_separate_messages() -> None:
    events = translated(
        chunk("agent_thought_chunk", "Hm"),
        chunk("agent_message_chunk", "Yes"),
        chunk("agent_message_chunk", "!"),
        chunk("agent_thought_chunk", "Done"),
    )
    assert events == [
        AssistantMessage(turn_id="t", text="Hm"),
        AssistantMessage(turn_id="t", text="Yes!"),
        AssistantMessage(turn_id="t", text="Done"),
    ]


def test_a_message_without_text_is_not_given() -> None:
    assert (
        translated(
            chunk("agent_message_chunk", ""),
            update("agent_message_chunk", content={"type": "image"}),
        )
        == []
    )


def test_a_tool_without_kiros_name_for_it_goes_by_its_title() -> None:
    events = translated(tool_call(_meta={}, rawInput={"__tool_use_purpose": "x", "path": "a"}))
    assert events == [
        ToolCallStarted(turn_id="t", call_id="c", name="Reading", arguments={"path": "a"})
    ]


def test_a_failed_tool_call_finishes_in_error() -> None:
    events = translated(finished(status="failed", rawOutput={"items": [{"Text": "No such file"}]}))
    assert events == [
        ToolCallFinished(turn_id="t", call_id="c", output="No such file", is_error=True)
    ]


def test_a_tool_outputs_text_items_are_joined_by_lines() -> None:
    raw_output = {"items": [{"Text": "one"}, {"Json": {}}, {"Text": "two"}]}
    events = translated(finished(status="completed", rawOutput=raw_output))
    assert events == [ToolCallFinished(turn_id="t", call_id="c", output="one\ntwo")]


def test_a_tool_call_finished_without_output_has_none() -> None:
    assert translated(finished(status="completed")) == [
        ToolCallFinished(turn_id="t", call_id="c", output="")
    ]


def test_only_credits_count_toward_a_turns_credits() -> None:
    metering = [{"value": 0.5, "unit": "credit"}, {"value": 7, "unit": "token"}]
    events = translated(
        line("metadata", meteringUsage=metering),
        line("metadata", meteringUsage=[{"value": 0.25, "unit": "credit"}]),
        line("runFinished", stopReason="end_turn"),
    )
    assert usage(events) == Usage(credits=0.75)


def test_reasoning_is_reported_unavailable_only_when_kiro_says_so() -> None:
    translation = translate_kiro("t", [line("metadata", reasoning={"support": "supported"})])
    list(translation)
    assert not translation.reasoning_unavailable
    assert translation.session_id is None


@pytest.mark.parametrize(
    "malformed",
    [
        line("runError", stage="prompt"),
        line("sessionUpdate"),
        update("tool_call", title="Reading"),
        update("agent_message_chunk"),
        json.dumps({"type": "metadata", "data": ["not", "an", "object"]}),
    ],
)
def test_a_known_event_missing_what_it_should_hold_is_unexpected_output(malformed: str) -> None:
    with pytest.raises(RuntimeError, match=r"^Unexpected output from kiro-cli: \{") as raised:
        translated(malformed)
    assert malformed[:40] in str(raised.value)
