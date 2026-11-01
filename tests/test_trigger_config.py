"""Triggers in the configuration: the package's, a project's overrides and its own, their checks
at load, the user config's refusal of them, and super_review_every as an alias for the super
review's every."""

from typing import Any

import pytest

from orbit.agent.domain.config import (
    SUPER_REVIEW_EVERY_MOVING,
    LayeredConfig,
    OrbitConfig,
    Package,
    load_config,
    load_layered,
)
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import DEFAULT, PACKAGE, Layer, load_user_roles
from orbit.agent.domain.roles import built_in_roles
from orbit.agent.domain.triggers import Trigger
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.agent.external.built_in_triggers import read_built_in_triggers
from orbit.protocol import BUILT_IN_ROLE_NAMES, SUPER_REVIEW

INSTRUCTIONS = {name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES}
ROLES = built_in_roles(read_built_in_roles(), INSTRUCTIONS)
SHIPPED = read_built_in_triggers()
PATH = "/src/app/.orbit/config.toml"
PROJECT = Layer("project", PATH)
NIGHTLY = {"on": "task_finished", "role": "coder", "prompt": "Tidy up"}
ROLE_NAMES = ", ".join(BUILT_IN_ROLE_NAMES)
"""The built-in roles' names, as a refusal lists them."""
LENS_TRIGGERS = tuple(
    f"{lens}-review" for lens in ("fit", "bugs", "tests", "logic", "security", "performance")
)
"""The shipped lens review triggers, in order."""
LENSES_OFF = {name: {"enabled": False} for name in LENS_TRIGGERS}
"""A project's triggers table disabling the shipped lens reviews."""
REVIEWS = {
    "on": "task_landed",
    "role": "reviewer",
    "prompt": "Review it.",
    "guards": ["review-rounds"],
}


def layered(data: dict[str, Any]) -> LayeredConfig:
    """The configuration a project file at :data:`PATH` holding ``data`` makes over the
    package's roles and triggers, with the layers its fields came from."""
    return load_layered(data, Package(ROLES, SHIPPED), path=PATH)


def loaded(data: dict[str, Any]) -> OrbitConfig:
    """The configuration a project file holding ``data`` makes over the package's roles and
    triggers."""
    return layered(data).config


def refusal(triggers: dict[str, Any], **data: Any) -> str:
    """Why a project file setting ``triggers`` and ``data`` is refused."""
    with pytest.raises(ConfigError) as raised:
        loaded({"triggers": triggers, **data})
    return str(raised.value)


# The package's triggers and a project's


def test_without_a_projects_triggers_they_are_the_packages_from_the_package() -> None:
    result = layered({})
    assert result.config.triggers == SHIPPED
    assert list(result.config.triggers) == [
        *LENS_TRIGGERS,
        "merge-pass",
        "super-review",
        "configure-checks",
        "set-up-verification",
        "startup-check",
        "startup-fix",
    ]
    review = result.trigger_layers["bugs-review"]
    assert (review["on"], review["role"], review["prompt"], review["guards"]) == (PACKAGE,) * 4
    assert (review["every"], review["manual"], review["enabled"]) == (DEFAULT,) * 3


def test_a_project_overrides_only_the_fields_it_sets() -> None:
    result = layered({"triggers": {SUPER_REVIEW: {"every": 5}}})
    assert result.config.triggers[SUPER_REVIEW] == SHIPPED[SUPER_REVIEW].model_copy(
        update={"every": 5}
    )
    assert result.trigger_layers[SUPER_REVIEW]["every"] == PROJECT
    assert result.trigger_layers[SUPER_REVIEW]["label"] == PACKAGE


def test_a_project_replaces_a_shipped_prompt_whole() -> None:
    config = loaded({"triggers": {"bugs-review": {"prompt": "Review {commit}."}}})
    assert config.triggers["bugs-review"].prompt == "Review {commit}."


def test_a_project_adds_its_own_trigger_after_the_shipped_ones() -> None:
    result = layered({"triggers": {"nightly": NIGHTLY}})
    assert list(result.config.triggers)[-1] == "nightly"
    assert result.config.triggers["nightly"] == Trigger(
        on="task_finished", role="coder", prompt="Tidy up"
    )
    assert result.trigger_layers["nightly"]["role"] == PROJECT
    assert result.trigger_layers["nightly"]["guards"] == DEFAULT


def test_a_project_can_disable_a_shipped_trigger() -> None:
    triggers = loaded({"triggers": {"bugs-review": {"enabled": False}}}).triggers
    assert not triggers["bugs-review"].enabled


def test_a_project_trigger_loads_without_the_packages_too() -> None:
    config = load_config({"triggers": {"nightly": NIGHTLY}}, ROLES)
    assert list(config.triggers) == ["nightly"]


def test_a_trigger_for_a_disabled_role_is_valid() -> None:
    config = loaded({"roles": {"coder": {"enabled": False}}, "triggers": {"nightly": NIGHTLY}})
    assert config.triggers["nightly"].role == "coder"


def test_a_trigger_taking_a_disabled_roles_tasks_is_valid() -> None:
    nightly = NIGHTLY | {"when": {"role": ["coder", "planner"]}}
    config = loaded({"roles": {"coder": {"enabled": False}}, "triggers": {"nightly": nightly}})
    assert config.triggers["nightly"].when == {"role": ("coder", "planner")}


def test_a_trigger_for_a_projects_own_role_is_valid() -> None:
    tidier = {"instructions": "Tidy.", "lane": "tidy", "workspace": "snapshot", "verify": False}
    nightly = NIGHTLY | {"role": "tidier"}
    config = loaded({"roles": {"tidier": tidier}, "triggers": {"nightly": nightly}})
    assert config.triggers["nightly"].role == "tidier"


# Checking them at load


@pytest.mark.parametrize(
    ("trigger", "problem"),
    [
        (
            NIGHTLY | {"on": "someday"},
            "triggers.nightly.on: someday isn't an event; the events are session_started,"
            " task_landed, task_finished, commits, startup_check_failed, manual, reviews_finished.",
        ),
        (
            NIGHTLY | {"role": "tidier"},
            f"triggers.nightly.role: tidier isn't a role; the roles are {ROLE_NAMES}.",
        ),
        (
            NIGHTLY | {"guards": ["weekly"]},
            "triggers.nightly.guards: weekly isn't a guard; the guards are review-rounds,"
            " suggestions-pending, not-queued, dedupe.",
        ),
        (
            NIGHTLY | {"when": {"branch": "main"}},
            "triggers.nightly.when: branch isn't a field of task_finished; its fields are role,"
            " origin, status.",
        ),
        (
            NIGHTLY | {"when": {"role": ["coder", "tidier"]}},
            f"triggers.nightly.when: tidier isn't a role; the roles are {ROLE_NAMES}.",
        ),
        (
            NIGHTLY | {"on": "session_started", "when": {"verification": "missng"}},
            "triggers.nightly.when: missng isn't a value of verification; its values are"
            " configured, missing, off.",
        ),
        (
            NIGHTLY | {"prompt": "Tidy {diff}"},
            "triggers.nightly.prompt: {diff} isn't a variable of task_finished; its variables are"
            " {trigger}.",
        ),
        (NIGHTLY | {"every": 3}, 'triggers.nightly.every: every = 3 needs on = "commits".'),
        (NIGHTLY | {"manual": True}, "triggers.nightly.label: manual = true needs a label."),
    ],
)
def test_an_invalid_trigger_is_refused_naming_it_its_field_and_the_value(
    trigger: dict[str, Any], problem: str
) -> None:
    assert refusal({"nightly": trigger}) == problem


def test_an_override_that_breaks_a_shipped_trigger_is_refused() -> None:
    assert refusal({"bugs-review": {"on": "task_finished"}}) == (
        "triggers.bugs-review.prompt: {title} isn't a variable of task_finished; its variables"
        " are {trigger}.\n"
        "triggers.bugs-review.prompt: {missing} isn't a variable of task_finished; its variables"
        " are {trigger}.\n"
        "triggers.bugs-review.prompt: {queued} isn't a variable of task_finished; its variables"
        " are {trigger}.\n"
        "triggers.bugs-review.prompt: {landed_since} isn't a variable of task_finished; its"
        " variables are {trigger}.\n"
        "triggers.bugs-review.prompt: {diff} isn't a variable of task_finished; its variables are"
        " {trigger}.\n"
        'triggers.bugs-review.guards: review-rounds needs on = "task_landed".'
    )


def test_review_rounds_off_a_landing_is_refused() -> None:
    mine = REVIEWS | {"on": "task_finished"}
    assert refusal({**LENSES_OFF, "mine": mine}) == (
        'triggers.mine.guards: review-rounds needs on = "task_landed".'
    )


def test_review_rounds_on_two_enabled_triggers_of_other_roles_loads() -> None:
    bugs = NIGHTLY | {"on": "task_landed", "guards": ["review-rounds"]}
    tests = bugs | {"prompt": "Test {commit}."}
    config = loaded({"triggers": {"bugs": bugs, "tests": tests}})
    guarded = [name for name, t in config.triggers.items() if "review-rounds" in t.guards]
    assert guarded == [*LENS_TRIGGERS, "bugs", "tests"]


def test_review_rounds_moves_to_a_trigger_of_your_own_with_the_lens_reviews_disabled() -> None:
    config = loaded({"triggers": {**LENSES_OFF, "mine": REVIEWS}})
    assert config.triggers["mine"].guards == ("review-rounds",)
    assert not any(config.triggers[name].enabled for name in LENS_TRIGGERS)


def test_a_disabled_second_review_rounds_trigger_loads() -> None:
    config = loaded({"triggers": {"spare": REVIEWS | {"enabled": False}}})
    assert not config.triggers["spare"].enabled


def test_the_lens_review_for_a_disabled_lens_reviewer_still_loads() -> None:
    config = loaded({"roles": {"bugs-reviewer": {"enabled": False}}})
    assert config.triggers["bugs-review"] == SHIPPED["bugs-review"]


def test_a_triggers_table_that_isnt_one_is_refused() -> None:
    with pytest.raises(ConfigError, match=r"^triggers: Input should be a valid dictionary"):
        loaded({"triggers": 5})
    assert refusal({"nightly": 5}).startswith("triggers.nightly: Input should be a valid")


def test_a_new_trigger_needs_an_event() -> None:
    assert refusal({"nightly": {"role": "coder", "prompt": "Go"}}) == (
        "triggers.nightly.on: Field required"
    )


def test_a_triggers_name_is_a_plain_name() -> None:
    assert refusal({"a.b": NIGHTLY}) == (
        "triggers.a.b: a trigger's name is a plain name: letters, digits, - and _"
    )


def test_the_user_config_cant_set_triggers() -> None:
    with pytest.raises(ConfigError) as raised:
        load_user_roles({"triggers": {"x": NIGHTLY}}, Layer("user"), ROLES)
    assert str(raised.value) == (
        "triggers: belongs in the project's .orbit/config.toml; the user config sets only a"
        " role's model, effort and runtime"
    )


# super_review_every, an alias for the super review's every


def test_super_review_every_alone_is_the_super_reviews_every_with_a_notice() -> None:
    result = layered({"super_review_every": 7})
    assert result.config.triggers[SUPER_REVIEW].every == 7
    assert result.trigger_layers[SUPER_REVIEW]["every"] == PROJECT
    assert result.config.notices == (SUPER_REVIEW_EVERY_MOVING,)


def test_the_super_reviews_own_every_wins_over_super_review_every_with_the_notice() -> None:
    config = loaded({"super_review_every": 7, "triggers": {SUPER_REVIEW: {"every": 5}}})
    assert config.triggers[SUPER_REVIEW].every == 5
    assert config.notices == (SUPER_REVIEW_EVERY_MOVING,)


def test_the_notice_says_where_super_review_every_is_moving() -> None:
    assert SUPER_REVIEW_EVERY_MOVING == (
        "super_review_every is moving to [triggers.super-review] every; set it there."
    )


def test_without_super_review_every_there_is_no_notice() -> None:
    config = loaded({})
    assert config.notices == ()
    assert config.triggers[SUPER_REVIEW].every == 10


def test_without_a_super_review_trigger_super_review_every_still_loads_with_the_notice() -> None:
    config = load_config({"super_review_every": 4}, ROLES)
    assert config.triggers == {}
    assert config.notices == (SUPER_REVIEW_EVERY_MOVING,)
