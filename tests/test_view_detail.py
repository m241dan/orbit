"""The detail pane shows a tool call's or request's full arguments and output."""

from rich.console import Group
from rich.rule import Rule
from rich.text import Text

from orbit.tui.view import (
    DETAIL_LINE_LIMIT,
    NO_DETAILS,
    WAITING,
    ToolCall,
    ToolCallStatus,
    argument_lines,
    capped,
    detail_body,
    detail_title,
)


def read_call(status: ToolCallStatus = "passed", output: str = "one\ntwo") -> ToolCall:
    return ToolCall("c", "Read", "Read src/x.py", {"file_path": "src/x.py"}, status, output)


def parts(call: ToolCall) -> tuple[Text, Rule, Text]:
    body = detail_body(call)
    assert isinstance(body, Group)
    arguments, rule, output = body.renderables
    assert isinstance(arguments, Text) and isinstance(rule, Rule) and isinstance(output, Text)
    return arguments, rule, output


def numbered(count: int) -> str:
    return "\n".join(f"line {i}" for i in range(count))


def test_the_title_names_the_calls_summary() -> None:
    assert detail_title(read_call()) == "Detail · Read src/x.py"


def test_the_title_without_a_call_is_just_detail() -> None:
    assert detail_title(None) == "Detail"


def test_no_call_shows_a_dim_note() -> None:
    body = detail_body(None)
    assert isinstance(body, Text)
    assert (body.plain, body.style) == (NO_DETAILS, "dim")


def test_a_finished_call_shows_its_arguments_a_rule_and_its_output() -> None:
    arguments, _, output = parts(read_call())
    assert arguments.plain == "file_path: src/x.py"
    assert output.plain == "one\ntwo"


def test_a_running_call_shows_its_arguments_then_waits_dimly() -> None:
    arguments, _, output = parts(read_call("running", ""))
    assert arguments.plain == "file_path: src/x.py"
    assert (output.plain, output.style) == (WAITING, "dim")


def test_arguments_keep_their_newlines_and_show_the_rest_as_json() -> None:
    lines = argument_lines({"command": "a\nb", "todos": [{"x": "é"}], "n": 3})
    assert lines.plain == 'command: a\nb\ntodos: [{"x": "é"}]\nn: 3'


def test_output_of_the_limit_is_shown_whole() -> None:
    output = numbered(DETAIL_LINE_LIMIT)
    assert capped(output).plain == output


def test_longer_output_keeps_its_ends_and_counts_the_lines_omitted() -> None:
    lines = capped(numbered(250)).plain.split("\n")
    assert len(lines) == 201
    assert lines[:2] == ["line 0", "line 1"] and lines[99] == "line 99"
    assert lines[100] == "… 50 lines omitted …"
    assert lines[101] == "line 150" and lines[-1] == "line 249"


def test_the_omitted_line_is_dim() -> None:
    text = capped(numbered(201))
    marker = text.plain.split("\n")[100]
    assert marker == "… 1 line omitted …"
    start = text.plain.index(marker)
    assert [span.style for span in text.spans if span.start == start] == ["dim"]
