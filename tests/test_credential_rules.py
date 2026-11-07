"""The rules of ``credentials.toml``: a flat table of strings, and the wording of refusals."""

from orbit.agent.domain.credentials import (
    NonStringCredential,
    parse_credentials,
    word_unset_because,
)


def test_a_table_of_strings_sets_each_variable() -> None:
    assert parse_credentials({"A": "1", "B": ""}) == {"A": "1", "B": ""}


def test_the_first_value_that_isnt_a_string_is_named() -> None:
    table = {"A": "1", "PORT": 8080, "NESTED": {"B": "2"}}
    assert parse_credentials(table) == NonStringCredential("PORT")


def test_a_variable_unset_because_of_the_file_says_both() -> None:
    assert word_unset_because("GITHUB_TOKEN", "f must be private: chmod 600 it.") == (
        "GITHUB_TOKEN isn't set in orbit's environment or credentials.toml."
        " f must be private: chmod 600 it."
    )
