"""Commented-out TOML lines, as orbit's reference configs write each setting."""

from orbit.agent.external.toml_comments import key_line, setting_line


def test_a_setting_without_a_value_is_written_unset() -> None:
    assert setting_line("clone_dir", None) == "# clone_dir: unset"


def test_a_setting_with_a_value_is_written_as_its_key_line() -> None:
    assert setting_line("limit", 3) == key_line("limit", 3) == "# limit = 3"


def test_a_table_value_is_written_inline() -> None:
    assert setting_line("env", {"A": "b"}) == '# env = {A = "b"}'
