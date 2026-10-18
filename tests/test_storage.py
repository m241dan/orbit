"""The storage settings and the long text fields cut from stored events."""

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.storage import StorageSettings, cut_fields, load_storage
from orbit.protocol import AssistantMessage, Notice, ToolCallFinished, ToolCallStarted

MARKER = "\n… 4 KB cut by orbit's tool_output_kb …\n"


def finished(output: str) -> ToolCallFinished:
    return ToolCallFinished(turn_id="t", call_id="c", output=output)


# The settings


def test_without_storage_the_defaults_apply() -> None:
    assert load_storage({}) == StorageSettings(tool_output_kb=64, session_log_mb=200)


def test_storage_is_read_over_the_defaults() -> None:
    settings = load_storage({"storage": {"tool_output_kb": 1}})
    assert settings == StorageSettings(tool_output_kb=1, session_log_mb=200)
    assert settings.tool_output_bytes == 1024


def test_zero_is_a_valid_setting() -> None:
    assert load_storage({"storage": {"tool_output_kb": 0, "session_log_mb": 0}}) == (
        StorageSettings(tool_output_kb=0, session_log_mb=0)
    )


def test_other_tables_are_ignored() -> None:
    assert load_storage({"server": {"port": 8080}, "name": "x"}) == StorageSettings()


@pytest.mark.parametrize(
    ("storage", "key"),
    [
        ({"tool_output": 3}, "storage.tool_output"),
        ({"tool_output_kb": -1}, "storage.tool_output_kb"),
        ({"tool_output_kb": "64"}, "storage.tool_output_kb"),
        ({"session_log_mb": 1.5}, "storage.session_log_mb"),
        ({"session_log_mb": True}, "storage.session_log_mb"),
    ],
)
def test_an_invalid_setting_raises_config_error_naming_the_key(
    storage: dict[str, object], key: str
) -> None:
    with pytest.raises(ConfigError, match=f"^{key}: "):
        load_storage({"storage": storage})


def test_a_storage_that_isnt_a_table_raises_config_error_naming_it() -> None:
    with pytest.raises(ConfigError, match=r"^storage: "):
        load_storage({"storage": 3})


# Cutting fields


def test_a_long_string_keeps_its_first_and_last_half_of_the_limit_around_a_marker() -> None:
    output = "a" * 2500 + "b" * 2500

    cut = cut_fields(finished(output), 1024)

    assert cut == finished("a" * 512 + MARKER + "b" * 512)


def test_a_string_no_longer_than_the_limit_is_kept_whole() -> None:
    event = finished("x" * 1024)
    assert cut_fields(event, 1024) == event


def test_fields_other_than_strings_are_kept() -> None:
    event = ToolCallFinished(turn_id="t", call_id="c", output="x" * 5000, is_error=True)
    cut = cut_fields(event, 1024)
    assert isinstance(cut, ToolCallFinished)
    assert cut.is_error


def test_strings_nested_in_tool_arguments_are_cut() -> None:
    long = "a" * 2500 + "b" * 2500
    event = ToolCallStarted(
        turn_id="t",
        call_id="c",
        name="Write",
        arguments={"content": long, "edits": [{"new": long}, 3], "pair": (long, None)},
    )

    cut = cut_fields(event, 1024)

    short = "a" * 512 + MARKER + "b" * 512
    assert isinstance(cut, ToolCallStarted)
    assert cut.arguments == {"content": short, "edits": [{"new": short}, 3], "pair": (short, None)}


def test_a_message_is_cut_too() -> None:
    cut = cut_fields(AssistantMessage(turn_id="t", text="z" * 5000), 1024)
    assert isinstance(cut, AssistantMessage)
    assert cut.text.startswith("z" * 512 + "\n… 4 KB cut")


def test_the_marker_names_a_large_cut_in_kb_and_a_huge_one_in_mb() -> None:
    kb = cut_fields(Notice(message="x" * 200_000), 1024)
    mb = cut_fields(Notice(message="x" * 3_000_000), 1024)
    assert isinstance(kb, Notice) and isinstance(mb, Notice)
    assert "\n… 194 KB cut by orbit's tool_output_kb …\n" in kb.message
    assert "\n… 3 MB cut by orbit's tool_output_kb …\n" in mb.message


def test_a_character_split_at_the_cut_is_dropped() -> None:
    cut = cut_fields(Notice(message="é" * 3000), 1023)
    assert isinstance(cut, Notice)
    head, tail = cut.message.split(" cut by orbit's tool_output_kb …\n")
    assert tail == "é" * 255
    assert head.startswith("é" * 255 + "\n… ")


def test_a_limit_of_zero_cuts_nothing() -> None:
    event = finished("x" * 100_000)
    assert cut_fields(event, 0) is event


def test_the_marker_names_a_small_cut_in_bytes() -> None:
    cut = cut_fields(Notice(message="x" * 1100), 1024)
    assert isinstance(cut, Notice)
    assert "\n… 76 bytes cut by orbit's tool_output_kb …\n" in cut.message
