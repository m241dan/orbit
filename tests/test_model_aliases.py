"""The models each runtime takes: its aliases, as the package's and the user config's tables
give them, and its full ids, and what a model resolves to among them."""

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.runtimes import (
    CLAUDE,
    ModelAliases,
    UnknownModel,
    known_models,
    load_model_tables,
    model_aliases,
    resolve_model,
    resolves,
)
from tests.agent_fakes import CLAUDE_MODELS

CLAUDES_ALIASES = (
    "the runtime claude takes the aliases fable, opus, sonnet, haiku or a full id starting with"
    ' "claude-"'
)
"""What says which models Claude Code takes, as orbit ships its aliases."""


def test_an_alias_resolves_to_its_target() -> None:
    fast = ModelAliases(CLAUDE, {"fast": "claude-haiku-5-5"}, "claude-")
    assert resolve_model(fast, "fast") == "claude-haiku-5-5"
    assert resolve_model(CLAUDE_MODELS, "opus") == "opus"


def test_a_full_id_resolves_to_itself() -> None:
    assert resolve_model(CLAUDE_MODELS, "claude-sonnet-5-5") == "claude-sonnet-5-5"


def test_an_alias_that_looks_like_a_full_id_resolves_to_its_target() -> None:
    renamed = ModelAliases(CLAUDE, {"claude-fast": "claude-haiku-5-5"}, "claude-")
    assert resolve_model(renamed, "claude-fast") == "claude-haiku-5-5"


def test_a_model_neither_an_alias_nor_a_full_id_doesnt_resolve_listing_the_aliases() -> None:
    with pytest.raises(UnknownModel) as raised:
        resolve_model(CLAUDE_MODELS, "sonet")
    assert str(raised.value) == f'The model "sonet" doesn\'t resolve: {CLAUDES_ALIASES}.'
    assert (raised.value.model, raised.value.models) == ("sonet", CLAUDE_MODELS)
    assert not resolves(CLAUDE_MODELS, "sonet")


def test_a_runtime_without_aliases_takes_only_full_ids() -> None:
    bare = ModelAliases("kiro", {}, "kiro-")
    assert known_models(bare) == 'the runtime kiro takes a full id starting with "kiro-"'
    assert resolves(bare, "kiro-auto")
    assert not resolves(bare, "auto")


def test_the_tables_give_each_runtimes_aliases_in_order_and_nothing_else() -> None:
    data = {
        "runtimes": {"claude": {"models": {"fast": "claude-haiku-5-5", "opus": "opus"}}},
        "storage": {"keep_sessions": 3},
    }
    tables = load_model_tables(data)
    assert tables == {CLAUDE: {"fast": "claude-haiku-5-5", "opus": "opus"}}
    assert list(tables[CLAUDE]) == ["fast", "opus"]


def test_without_tables_no_runtime_has_aliases() -> None:
    assert load_model_tables({}) == {}
    assert load_model_tables({"runtimes": {"claude": {}}}) == {CLAUDE: {}}


def test_tables_for_a_runtime_this_version_doesnt_know_are_refused_naming_each() -> None:
    data = {"runtimes": {"nope": {"models": {"x": "y"}}, "claude": {}, "kiro": {}}}
    with pytest.raises(ConfigError) as raised:
        load_model_tables(data)
    assert str(raised.value).splitlines() == [
        "runtimes.nope: this version of orbit knows the runtimes claude.",
        "runtimes.kiro: this version of orbit knows the runtimes claude.",
    ]


@pytest.mark.parametrize(
    ("runtime", "key"),
    [
        ({"models": {"fast": 5}}, "runtimes.claude.models.fast"),
        ({"models": {"fast": ""}}, "runtimes.claude.models.fast"),
        ({"models": {"": "claude-haiku-5-5"}}, "runtimes.claude.models."),
        ({"models": "fast"}, "runtimes.claude.models"),
        ({"aliases": {}}, "runtimes.claude.aliases"),
    ],
)
def test_an_invalid_table_is_refused_naming_its_key(runtime: object, key: str) -> None:
    with pytest.raises(ConfigError) as raised:
        load_model_tables({"runtimes": {"claude": runtime}})
    assert str(raised.value).startswith(f"{key}: ")


def test_the_users_aliases_are_added_to_the_packages_or_take_their_place() -> None:
    package = {CLAUDE: {"opus": "opus", "haiku": "haiku"}}
    user = {CLAUDE: {"fast": "claude-haiku-5-5", "opus": "claude-opus-5-5"}}
    models = model_aliases(package, user, {CLAUDE: "claude-"})
    aliases = {"opus": "claude-opus-5-5", "haiku": "haiku", "fast": "claude-haiku-5-5"}
    assert models == {CLAUDE: ModelAliases(CLAUDE, aliases, "claude-")}
    assert list(models[CLAUDE].aliases) == ["opus", "haiku", "fast"]


def test_a_runtime_neither_table_gives_aliases_takes_only_its_full_ids() -> None:
    models = model_aliases({}, {}, {CLAUDE: "claude-", "kiro": "kiro-"})
    assert models == {
        CLAUDE: ModelAliases(CLAUDE, {}, "claude-"),
        "kiro": ModelAliases("kiro", {}, "kiro-"),
    }
