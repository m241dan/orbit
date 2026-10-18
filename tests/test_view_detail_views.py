"""The detail pane shows each tool's call in the form that suits it."""

import io
from dataclasses import replace
from typing import Any

from rich.console import Console, RenderableType
from rich.segment import Segment
from rich.text import Text

from orbit.tui.view.detail import DETAIL_LINE_LIMIT, EMPTY_DIFF, NO_DETAILS, WAITING
from orbit.tui.view.detail_views import detail_body, diff_text, pretty_json, read_output
from orbit.tui.view.tool_calls import ToolCall, ToolCallStatus

RULE = "─" * 60
READ_OUTPUT = (
    "    41→def go(x: int) -> int:\n"
    '    42→    """Go."""\n'
    "    43→\n"
    "    44→    return x\n"
    "\n"
    "<system-reminder>\n"
    "Whenever you read a file, check it.\n"
    "</system-reminder>"
)


def call(
    name: str,
    arguments: dict[str, Any],
    output: str = "",
    status: ToolCallStatus = "passed",
    request: bool = False,
) -> ToolCall:
    return ToolCall("c", name, name, arguments, status, output, request)


def plain(renderable: RenderableType) -> list[str]:
    """``renderable``'s lines as drawn 60 columns wide, without styles."""
    out = io.StringIO()
    Console(width=60, file=out, color_system=None).print(renderable)
    return [line.rstrip() for line in out.getvalue().splitlines()]


def segments(renderable: RenderableType) -> list[Segment]:
    """``renderable``'s styled pieces as drawn 60 columns wide."""
    console = Console(width=60, file=io.StringIO(), color_system="truecolor")
    return list(console.render(renderable))


def style_of(renderable: RenderableType, text: str) -> str:
    """The style of the first piece of ``renderable`` reading ``text``."""
    return next(str(piece.style) for piece in segments(renderable) if piece.text == text)


def numbered(count: int) -> str:
    return "\n".join(f"line {i}" for i in range(count))


def test_no_call_shows_a_dim_note() -> None:
    body = detail_body(None)
    assert isinstance(body, Text)
    assert (body.plain, body.style) == (NO_DETAILS, "dim")


def test_read_shows_the_path_then_the_numbered_lines_and_what_follows() -> None:
    body = detail_body(call("Read", {"file_path": "src/go.py"}, READ_OUTPUT))
    assert plain(body) == [
        "src/go.py",
        RULE,
        "  41 def go(x: int) -> int:",
        '  42     """Go."""',
        "  43",
        "  44     return x",
        "",
        "<system-reminder>",
        "Whenever you read a file, check it.",
        "</system-reminder>",
    ]


def test_read_highlights_code_by_the_files_extension() -> None:
    body = detail_body(call("Read", {"file_path": "src/go.py"}, READ_OUTPUT))
    assert style_of(body, "def") == "bright_blue on default"
    assert style_of(body, "41 ") == "dim"


def test_read_of_an_unknown_file_type_keeps_numbers_but_no_highlighting() -> None:
    body = read_output("notes.zzz", "     1\tdef x\n     2\tend")
    assert plain(body) == ["  1 def x", "  2 end"]
    assert style_of(body, "def x") == "on default"


def test_read_names_the_offset_and_limit_dimly() -> None:
    body = detail_body(call("Read", {"file_path": "a.py", "offset": 41, "limit": 4}, "x"))
    assert plain(body)[0] == "a.py (offset 41, limit 4)"
    assert style_of(body, "a.py") == "bold"
    assert style_of(body, " (offset 41, limit 4)") == "dim"


def test_read_output_without_line_numbers_is_plain() -> None:
    assert plain(read_output("a.py", "one\ntwo")) == ["one", "two"]


def test_read_numbers_lines_past_a_form_feed() -> None:
    body = read_output("a.py", "     1→a = 1\n     2→b\x0cc\n     3→d = 3\n")
    numbered = [line[:4] for line in plain(body) if line[:4] in ("  1 ", "  2 ", "  3 ")]
    assert numbered == ["  1 ", "  2 ", "  3 "]
    assert "→" not in "".join(plain(body))


def test_read_of_a_long_file_numbers_each_end_from_its_own_lines() -> None:
    output = "\n".join(f"{n:6}→x = {n}" for n in range(1, 301))
    lines = plain(detail_body(call("Read", {"file_path": "a.py"}, output)))
    assert lines[2] == "    1 x = 1" and lines[101] == "  100 x = 100"
    assert lines[102] == "… 100 lines omitted · y copies all …"
    assert lines[103] == "  201 x = 201" and lines[-1] == "  300 x = 300"


def test_read_of_a_long_file_numbers_each_end_past_a_form_feed() -> None:
    lines = [f"{n:6}→x = {n}" for n in range(1, 301)]
    lines[4] = "     5→a\x0cb"
    drawn = plain(detail_body(call("Read", {"file_path": "a.py"}, "\n".join(lines))))
    shown = {line.split()[0] for line in drawn if line.split() and line.split()[0].isdigit()}
    assert {str(n) for n in [*range(1, 101), *range(201, 301)]} <= shown
    assert "→" not in "".join(drawn)


def test_bash_shows_the_whole_command_then_its_output() -> None:
    body = detail_body(call("Bash", {"command": "cd x &&\n  ls"}, "a\nb"))
    assert plain(body) == ["$ cd x &&", "  ls", RULE, "a", "b"]


def test_bash_keeps_the_outputs_colours() -> None:
    body = detail_body(call("Bash", {"command": "ls"}, "\x1b[31mred\x1b[0m plain"))
    assert plain(body)[-1] == "red plain"
    assert style_of(body, "red") == "color(1)"


def test_grep_shows_the_pattern_and_path_then_the_matches() -> None:
    body = detail_body(call("Grep", {"pattern": "def go", "path": "src"}, "a.py\nb.py"))
    assert plain(body) == ["def go in src", RULE, "a.py", "b.py"]
    assert style_of(body, "def go") == "bold"


def test_glob_without_a_path_shows_just_the_pattern() -> None:
    body = detail_body(call("Glob", {"pattern": "**/*.py"}, "a.py"))
    assert plain(body) == ["**/*.py", RULE, "a.py"]


def test_other_tools_show_their_arguments_as_highlighted_json() -> None:
    body = detail_body(call("WebSearch", {"query": "rich", "n": 3}, "Found it"))
    assert plain(body) == ["{", '  "query": "rich",', '  "n": 3', "}", RULE, "Found it"]
    assert style_of(body, '"query"') != style_of(body, '"rich"')


def test_json_output_is_indented_and_highlighted() -> None:
    body = detail_body(call("mcp__x__list", {}, '{"items": [1]}'))
    assert plain(body)[-5:] == ["{", '  "items": [', "    1", "  ]", "}"]
    assert style_of(body, "1") != "on default"


def test_pretty_json_leaves_text_and_bare_values_alone() -> None:
    assert pretty_json("not json") is None
    assert pretty_json("42") is None
    assert pretty_json('["é"]') == '[\n  "é"\n]'


def test_pretty_json_leaves_too_deeply_nested_text_alone() -> None:
    assert pretty_json("[" * 100000) is None


def test_too_deeply_nested_output_is_shown_as_text() -> None:
    body = detail_body(call("WebFetch", {}, "[" * 100000))
    assert "".join(plain(body)[1:]) == "[" * 100000


def test_a_running_call_shows_its_header_then_waits_dimly() -> None:
    body = detail_body(call("Bash", {"command": "ls"}, status="running"))
    assert plain(body) == ["$ ls", RULE, WAITING]
    assert style_of(body, WAITING) == "dim"


def test_a_request_shows_its_arguments_then_orbits_reply() -> None:
    body = detail_body(call("complete_task", {"summary": "Go"}, "Merged.", request=True))
    assert plain(body) == ["{", '  "summary": "Go"', "}", RULE, "orbit: Merged."]
    assert style_of(body, "orbit: Merged.") == "None"


def test_a_failed_request_shows_orbits_reply_in_red() -> None:
    failed = call("complete_task", {}, "Checks failed.", "failed", request=True)
    assert style_of(detail_body(failed), "orbit: Checks failed.") == "red"


def test_a_requests_long_reply_is_capped() -> None:
    body = detail_body(call("give_up", {}, numbered(DETAIL_LINE_LIMIT + 1), request=True))
    lines = plain(body)
    assert lines[1] == "orbit: line 0" and lines[-1] == "line 200"
    assert "… 1 line omitted · y copies all …" in lines


def test_a_bash_calls_long_output_is_capped_before_drawing() -> None:
    lines = plain(detail_body(call("Bash", {"command": "seq"}, numbered(250))))
    assert len(lines) == 2 + 201 and lines[102] == "… 50 lines omitted · y copies all …"


def test_malformed_arguments_fall_back_to_json() -> None:
    for name in ("Read", "Bash", "Grep"):
        body = detail_body(call(name, {"file_path": 1, "command": None}, "out"))
        assert plain(body)[:2] == ["{", '  "file_path": 1,'] and plain(body)[-1] == "out"


def test_arguments_that_arent_json_are_shown_as_text() -> None:
    body = detail_body(call("Odd", {"when": object}, "out"))
    assert plain(body)[1] == '  "when": "<class \'object\'>"'


def edit(old: str, new: str, output: str = "Updated.", **more: Any) -> ToolCall:
    arguments = {"file_path": "a.py", "old_string": old, "new_string": new, **more}
    return call("Edit", arguments, output)


def test_an_edit_shows_the_path_its_diff_then_its_output() -> None:
    body = detail_body(edit("a\nb\nc", "a\nB\nc"))
    assert plain(body) == ["a.py", RULE, " a", "-b", "+B", " c", RULE, "Updated."]
    assert style_of(body, "a.py") == "bold"


def test_an_edits_lines_are_red_removed_green_added_and_dim_unchanged() -> None:
    body = detail_body(edit("a\nb", "a\nB"))
    assert style_of(body, "-b") == "red"
    assert style_of(body, "+B") == "green"
    assert style_of(body, " a") == "dim"
    assert style_of(body, "Updated.") == "dim"


def test_replace_all_doesnt_change_an_edits_view() -> None:
    assert plain(detail_body(edit("x", "y", replace_all=True))) == plain(
        detail_body(edit("x", "y"))
    )


def test_a_failed_edits_output_is_red() -> None:
    failed = replace(edit("x", "y", "String not found"), status="failed")
    assert style_of(detail_body(failed), "String not found") == "red"


def test_a_running_edit_shows_its_diff_then_waits() -> None:
    running = replace(edit("x", "y"), status="running")
    assert plain(detail_body(running))[-1] == WAITING


def test_a_multi_edit_separates_its_edits_dimly() -> None:
    edits = [{"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "d"}]
    body = detail_body(call("MultiEdit", {"file_path": "a.py", "edits": edits}, "Done."))
    assert plain(body) == ["a.py", RULE, "-a", "+b", "⋯", "-c", "+d", RULE, "Done."]
    assert style_of(body, "⋯") == "dim"


def test_a_write_shows_all_its_content_added() -> None:
    body = detail_body(call("Write", {"file_path": "a.py", "content": "x\ny"}, "Created."))
    assert plain(body) == ["a.py", RULE, "1 +x", "2 +y", RULE, "Created."]
    assert style_of(body, "+y") == "green"
    assert style_of(body, "2 ") == "dim"


def test_an_empty_write_notes_dimly_that_no_lines_changed() -> None:
    body = detail_body(call("Write", {"file_path": "a.py", "content": ""}, "Created."))
    assert plain(body) == ["a.py", RULE, EMPTY_DIFF, RULE, "Created."]
    assert style_of(body, EMPTY_DIFF) == "dim"


def test_a_multi_edit_without_edits_notes_that_no_lines_changed() -> None:
    body = detail_body(call("MultiEdit", {"file_path": "a.py", "edits": []}, "Done."))
    assert plain(body) == ["a.py", RULE, EMPTY_DIFF, RULE, "Done."]


def test_a_long_diff_is_capped() -> None:
    content = "\n".join(f"x{i}" for i in range(250))
    lines = plain(detail_body(call("Write", {"file_path": "a.py", "content": content}, "ok")))
    assert lines[2] == "  1 +x0" and lines[101] == "100 +x99"
    assert lines[102] == "… 50 lines omitted · y copies all …"
    assert lines[103] == "151 +x150" and lines[-3:] == ["250 +x249", RULE, "ok"]


def test_malformed_edits_fall_back_to_json() -> None:
    for name in ("Edit", "MultiEdit", "Write"):
        body = detail_body(call(name, {"file_path": "a.py"}, "out"))
        assert plain(body) == ["{", '  "file_path": "a.py"', "}", RULE, "out"]


def test_diff_text_without_lines_is_empty() -> None:
    assert diff_text("").plain == ""


EDITED = (
    "The file a.py has been updated. Here's the result of running `cat -n` on a snippet of the"
    " edited file:\n     7→a\n     8→B\n     9→c"
)


def test_an_edit_found_in_its_result_numbers_its_new_lines_dimly() -> None:
    body = detail_body(edit("a\nb\nc", "a\nB\nc", EDITED))
    assert plain(body)[2:6] == ["7  a", "  -b", "8 +B", "9  c"]
    assert style_of(body, "8 ") == "dim"
    assert style_of(body, "+B") == "green"


def test_a_failed_edit_has_no_numbers() -> None:
    failed = replace(edit("a\nb\nc", "a\nB\nc", EDITED), status="failed")
    assert plain(detail_body(failed))[2:6] == [" a", "-b", "+B", " c"]


def test_diff_text_keeps_a_line_holding_a_form_feed_whole() -> None:
    text = diff_text("+a\x0cb\n c")
    assert text.plain == "+ab\n c"
    assert [(span.start, span.end, span.style) for span in text.spans] == [
        (0, 3, "green"),
        (4, 6, "dim"),
    ]


def test_diff_text_styles_lines_after_their_numbers() -> None:
    text = diff_text("1 +x", gutter=2)
    assert [(span.start, span.end, span.style) for span in text.spans] == [
        (0, 2, "dim"),
        (2, 4, "green"),
    ]


def test_a_call_without_arguments_begins_with_the_rule() -> None:
    assert plain(detail_body(call("mcp__x__ping", {}, "pong"))) == [RULE, "pong"]


def test_a_request_without_arguments_begins_with_the_rule() -> None:
    body = detail_body(call("get_task", {}, "Do it.", request=True))
    assert plain(body) == [RULE, "orbit: Do it."]
