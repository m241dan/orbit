"""The detail pane shows a tool call's or request's full arguments and output."""

from rich.console import Group
from rich.text import Text

from orbit.tui.view import (
    DETAIL_COVER_BELOW,
    DETAIL_LINE_LIMIT,
    ToolCall,
    ToolCallStatus,
    capped_with,
    copied_note,
    detail_copy,
    detail_covers,
    detail_title,
)


def read_call(status: ToolCallStatus = "passed", output: str = "one\ntwo") -> ToolCall:
    return ToolCall("c", "Read", "Read src/x.py", {"file_path": "src/x.py"}, status, output)


def numbered(count: int) -> str:
    return "\n".join(f"line {i}" for i in range(count))


def test_the_pane_covers_a_conversation_shorter_than_the_minimum() -> None:
    assert detail_covers(DETAIL_COVER_BELOW - 1)


def test_the_pane_leaves_room_in_a_conversation_of_the_minimum_height() -> None:
    assert not detail_covers(DETAIL_COVER_BELOW)


def test_the_title_names_the_calls_summary() -> None:
    assert detail_title(read_call()) == "Detail · Read src/x.py · y copy"


def test_the_title_without_a_call_is_just_detail() -> None:
    assert detail_title(None) == "Detail"


def test_the_omitted_line_counts_one_line_singly_and_dimly() -> None:
    drawn = capped_with(numbered(201), Text)
    assert isinstance(drawn, Group)
    marker = drawn.renderables[1]
    assert isinstance(marker, Text)
    assert (marker.plain, marker.style) == ("… 1 line omitted · y copies all …", "dim")


def test_capped_with_draws_output_of_the_limit_whole() -> None:
    output = numbered(DETAIL_LINE_LIMIT)
    assert capped_with(output, lambda text: text.upper()) == output.upper()


def test_capped_with_draws_each_end_of_longer_output_around_the_dim_count() -> None:
    drawn = capped_with(numbered(250), lambda text: Text(text.upper()))
    assert isinstance(drawn, Group)
    head, marker, tail = drawn.renderables
    assert isinstance(head, Text) and isinstance(marker, Text) and isinstance(tail, Text)
    assert head.plain.split("\n")[-1] == "LINE 99" and tail.plain.split("\n")[0] == "LINE 150"
    assert (marker.plain, marker.style) == ("… 50 lines omitted · y copies all …", "dim")


def test_y_copies_a_finished_calls_whole_output() -> None:
    output = numbered(250)
    assert detail_copy(read_call(output=output)) == output


def test_y_copies_a_running_calls_arguments_as_json() -> None:
    call = ToolCall("c", "Bash", "Bash ls", {"command": "ls é", "n": 1})
    assert detail_copy(call) == '{\n  "command": "ls é",\n  "n": 1\n}'


def test_y_copies_the_arguments_of_a_call_that_lost_its_result() -> None:
    call = ToolCall("c", "Bash", "Bash ls", {"command": "ls"}, "no_result")
    assert detail_copy(call) == '{\n  "command": "ls"\n}'


def test_y_copies_the_arguments_of_a_finished_call_with_no_output() -> None:
    call = ToolCall("c", "Bash", "Bash true", {"command": "true"}, "passed", "")
    assert detail_copy(call) == '{\n  "command": "true"\n}'


def test_y_copies_an_edits_whole_diff() -> None:
    content = numbered(250)
    call = ToolCall("c", "Write", "Write a.py", {"file_path": "a.py", "content": content})
    assert detail_copy(call) == "\n".join(f"+{line}" for line in content.splitlines())


def test_y_copies_a_finished_edits_diff_rather_than_its_output() -> None:
    arguments = {"file_path": "a.py", "old_string": "a", "new_string": "b"}
    call = ToolCall("c", "Edit", "Edit a.py", arguments, "passed", "Updated.")
    assert detail_copy(call) == "-a\n+b"


def test_y_copies_a_finished_empty_writes_output() -> None:
    arguments = {"file_path": "a.py", "content": ""}
    call = ToolCall("c", "Write", "Write a.py", arguments, "passed", "Created.")
    assert detail_copy(call) == "Created."


def test_y_copies_a_running_empty_writes_arguments_as_json() -> None:
    call = ToolCall("c", "Write", "Write a.py", {"file_path": "a.py", "content": ""})
    assert detail_copy(call) == '{\n  "file_path": "a.py",\n  "content": ""\n}'


def test_y_copies_a_request_named_like_an_edit_by_its_reply() -> None:
    arguments = {"file_path": "a.py", "content": "x"}
    call = ToolCall("c", "Write", "Write", arguments, "passed", "Merged.", request=True)
    assert detail_copy(call) == "Merged."


def test_y_copies_nothing_without_a_call() -> None:
    assert detail_copy(None) is None


def test_the_copied_note_counts_lines() -> None:
    assert copied_note("a\nb") == "Copied 2 lines"


def test_the_copied_note_counts_one_line_singly() -> None:
    assert copied_note("a") == "Copied 1 line"


def test_capped_output_ending_in_a_newline_has_no_trailing_blank_line() -> None:
    drawn = capped_with(numbered(250) + "\n", Text)
    assert isinstance(drawn, Group)
    tail = drawn.renderables[-1]
    assert isinstance(tail, Text) and tail.plain.split("\n")[-1] == "line 249"
