"""Triggers: the events and guards orbit knows, a trigger's fields, the package's triggers merged
with a project's and checked against its roles, and the rendering of a prompt template."""

from typing import Any

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import DEFAULT, PACKAGE, Layer
from orbit.agent.domain.triggers import (
    EVENT_NAMES,
    EVENTS,
    GUARD_NAMES,
    WHEN_VALUES,
    SetOutcome,
    Trigger,
    TriggerEvent,
    check_triggers,
    load_built_in_triggers,
    merge_triggers,
    render_template,
    set_finished_event,
    set_finished_values,
    template_variables,
    trigger_tab,
)
from orbit.agent.domain.verification import OUTPUT_LIMIT, trim_output

PROJECT = Layer("project", "/src/app/.orbit/config.toml")
ROLES = ("coder", "reviewer")
REVIEW = Trigger(
    on="task_landed", role="reviewer", prompt="Review {commit}", guards=("review-rounds",)
)
CHECK = Trigger(on="session_started", when={"verification": "configured"}, checks="task-checks")
PACKAGE_TRIGGERS = {"review": REVIEW, "check": CHECK}


def loaded(fields: dict[str, Any]) -> Trigger:
    """The trigger ``x`` of a ``triggers.toml`` giving it ``fields``."""
    return load_built_in_triggers({"triggers": {"x": fields}})["x"]


def problems(**fields: Any) -> str:
    """Why the trigger ``x`` with ``fields`` is refused against :data:`ROLES`."""
    with pytest.raises(ConfigError) as raised:
        check_triggers({"x": Trigger(**fields)}, ROLES)
    return str(raised.value)


# Events and guards


def test_the_events_are_the_records_with_their_when_fields() -> None:
    assert EVENT_NAMES == (
        "session_started",
        "task_landed",
        "task_finished",
        "commits",
        "startup_check_failed",
        "manual",
        "reviews_finished",
        "set_finished",
        "schedule",
    )
    assert {name: event.fields for name, event in EVENTS.items()} == {
        "session_started": ("verification",),
        "task_landed": ("role", "origin", "review"),
        "task_finished": ("role", "origin", "status"),
        "commits": ("role", "origin"),
        "startup_check_failed": ("step",),
        "manual": (),
        "reviews_finished": ("findings", "review"),
        "set_finished": ("set", "status"),
        "schedule": (),
    }


def test_verification_findings_and_review_have_values_fixed_by_orbit_on_each_event() -> None:
    assert WHEN_VALUES == {
        "session_started": {"verification": ("configured", "missing", "off")},
        "task_landed": {"review": ("commit", "batch")},
        "reviews_finished": {
            "findings": ("some", "none"),
            "review": ("commit", "batch", "super", "set"),
        },
        "set_finished": {"status": ("passed", "failed")},
    }


def test_every_event_gives_its_trigger_and_its_own_variables() -> None:
    assert {name: event.variables for name, event in EVENTS.items()} == {
        "session_started": ("trigger", "checks"),
        "task_landed": (
            "trigger",
            "commit",
            "task",
            "title",
            "diff",
            "missing",
            "cap",
            "queued",
            "landed_since",
            "task_skills",
        ),
        "task_finished": ("trigger",),
        "commits": (
            "trigger",
            "commit",
            "cap",
            "queued",
            "since_commits",
            "changed_files",
            "sample",
        ),
        "startup_check_failed": ("trigger", "step", "output"),
        "manual": ("trigger", "checks"),
        "reviews_finished": (
            "trigger",
            "title",
            "commit",
            "findings",
            "cap",
            "queued",
            "landed_since",
            "messages",
        ),
        "set_finished": ("trigger", "set", "status", "commit", "output"),
        "schedule": ("trigger",),
    }


def test_set_finished_gives_a_passing_runs_set_status_and_commit_and_no_output() -> None:
    outcome = SetOutcome("mutation", "1a2b3c4d5e")
    assert set_finished_event(outcome) == TriggerEvent(
        "set_finished", {"set": "mutation", "status": "passed"}
    )
    assert set_finished_values(outcome) == {
        "set": "mutation",
        "status": "passed",
        "commit": "1a2b3c4",
        "output": "",
    }


def test_set_finished_gives_a_failing_runs_step_and_output_as_its_output() -> None:
    outcome = SetOutcome("mutation", "1a2b3c4d5e", ("Mutation tests", "  3 survived \n"))
    assert set_finished_event(outcome).fields == {"set": "mutation", "status": "failed"}
    assert set_finished_values(outcome)["output"] == (
        "Mutation tests failed. Its output:\n\n3 survived"
    )


def test_a_failing_runs_output_says_when_there_was_none() -> None:
    outcome = SetOutcome("mutation", "1a2b3c4", ("Mutation tests", " \n"))
    assert set_finished_values(outcome)["output"] == "Mutation tests failed, with no output."


def test_a_failing_runs_output_is_trimmed_as_the_panel_trims_it() -> None:
    long = "x" * (OUTPUT_LIMIT + 100)
    outcome = SetOutcome("mutation", "1a2b3c4", ("Mutation tests", long))
    output = set_finished_values(outcome)["output"]
    assert output == f"Mutation tests failed. Its output:\n\n{trim_output(long)}"


def test_the_guards_are_the_records() -> None:
    assert GUARD_NAMES == (
        "review-rounds",
        "raised-finding",
        "suggestions-pending",
        "not-queued",
        "dedupe",
    )


# A trigger's fields


def test_a_trigger_leaves_out_what_it_doesnt_need() -> None:
    trigger = Trigger(on="task_finished", role="coder", prompt="Tidy up")
    assert trigger.when == {}
    assert trigger.guards == ()
    assert (trigger.every, trigger.checks, trigger.label) == (None, None, None)
    assert (trigger.manual, trigger.draft, trigger.enabled) == (False, False, True)


def test_when_takes_a_value_or_a_list_of_values() -> None:
    trigger = loaded(
        {"on": "commits", "role": "coder", "prompt": "Go", "when": {"origin": ["manual"]}}
    )
    assert trigger.when == {"origin": ("manual",)}
    assert loaded({"on": "task_landed", "checks": "slow", "when": {"role": "coder"}}).when == {
        "role": "coder"
    }


def test_every_field_has_a_description() -> None:
    assert all(field.description for field in Trigger.model_fields.values())


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        (
            {"on": "landed"},
            "triggers.x.on: landed isn't an event; the events are session_started, task_landed,"
            " task_finished, commits, startup_check_failed, manual, reviews_finished,"
            " set_finished, schedule.",
        ),
        (
            {"on": "commits", "guards": ["review-rounds", "nightly"]},
            "triggers.x.guards: nightly isn't a guard; the guards are review-rounds,"
            " raised-finding, suggestions-pending, not-queued, dedupe.",
        ),
        ({"on": "commits", "every": 0}, "triggers.x.every: Input should be greater than 0"),
        ({"on": "commits", "colour": "red"}, "triggers.x.colour: Extra inputs are not permitted"),
        ({"on": "commits", "checks": 3}, "triggers.x.checks: Input should be a valid string"),
    ],
)
def test_a_trigger_is_refused_naming_its_field_and_value(
    fields: dict[str, Any], problem: str
) -> None:
    with pytest.raises(ConfigError) as raised:
        loaded(fields)
    assert str(raised.value) == problem


def test_a_triggers_name_is_a_plain_name() -> None:
    with pytest.raises(ConfigError) as raised:
        load_built_in_triggers({"triggers": {"a.b": {"on": "manual"}}})
    assert str(raised.value) == (
        "triggers.a.b: a trigger's name is a plain name: letters, digits, - and _"
    )


def test_a_tasks_tab_is_that_of_the_trigger_its_origin_names() -> None:
    tabbed = {"review": REVIEW.model_copy(update={"tab": "review"}), "check": CHECK}
    assert trigger_tab(tabbed, "review") == "review"


def test_a_task_has_no_tab_when_its_origin_names_no_trigger_or_one_without() -> None:
    assert (trigger_tab(PACKAGE_TRIGGERS, "planner"), trigger_tab(PACKAGE_TRIGGERS, "check")) == (
        None,
        None,
    )


def test_the_package_keeps_its_triggers_in_order() -> None:
    data = {
        "triggers": {
            "b": {"on": "manual", "role": "coder", "prompt": "b.md"},
            "a": {"on": "commits", "checks": "task-checks"},
        }
    }
    assert list(load_built_in_triggers(data)) == ["b", "a"]


# Checking triggers against the roles


def test_valid_triggers_pass() -> None:
    check_triggers(PACKAGE_TRIGGERS, ROLES)


def test_an_unknown_role_is_refused_naming_it_and_the_roles() -> None:
    assert problems(on="task_finished", role="tidier", prompt="Tidy") == (
        "triggers.x.role: tidier isn't a role; the roles are coder, reviewer."
    )


def test_a_trigger_needs_a_role_or_checks() -> None:
    assert problems(on="task_finished") == (
        'triggers.x.role: a trigger needs a role, or checks = "task-checks" or a set.'
    )


def test_a_trigger_cant_have_both_a_role_and_checks() -> None:
    assert problems(on="task_finished", role="coder", prompt="Go", checks="task-checks") == (
        "triggers.x.checks: a trigger has a role or checks, not both."
    )


def test_a_trigger_runs_the_task_checks_or_a_set_a_step_is_in() -> None:
    triggers = {
        "tasks": Trigger(on="commits", checks="task-checks"),
        "slow": Trigger(on="commits", checks="slow"),
    }
    check_triggers(triggers, ROLES, ("slow", "fast"))


def test_a_trigger_running_a_set_no_step_is_in_is_refused_naming_the_sets() -> None:
    with pytest.raises(ConfigError) as raised:
        check_triggers({"x": Trigger(on="commits", checks="slwo")}, ROLES, ("slow", "fast"))
    assert str(raised.value) == (
        'triggers.x.checks: slwo isn\'t "task-checks" or a set a step is in; the sets are slow,'
        " fast."
    )


def test_a_trigger_running_a_set_without_sets_is_refused_saying_there_are_none() -> None:
    assert problems(on="commits", checks="slow") == (
        'triggers.x.checks: slow isn\'t "task-checks" or a set a step is in; no step is in a set.'
    )


def test_gate_is_read_as_the_task_checks() -> None:
    assert loaded({"on": "session_started", "checks": "gate"}).checks == "task-checks"


def test_a_trigger_with_a_role_needs_a_prompt() -> None:
    assert problems(on="task_finished", role="coder") == (
        "triggers.x.prompt: a trigger with a role needs a prompt."
    )


def test_a_checks_trigger_takes_no_prompt() -> None:
    assert problems(on="manual", checks="gate", prompt="Go", manual=True, label="Check") == (
        "triggers.x.prompt: checks queues no task, so it takes no prompt."
    )


def test_a_when_field_its_event_lacks_is_refused_naming_it_and_the_fields() -> None:
    assert problems(on="task_landed", checks="gate", when={"status": "done"}) == (
        "triggers.x.when: status isn't a field of task_landed; its fields are role, origin, review."
    )


def test_a_when_field_of_an_event_without_fields_is_refused() -> None:
    assert problems(on="manual", checks="gate", manual=True, label="C", when={"role": "x"}) == (
        "triggers.x.when: role isn't a field of manual; it has none."
    )


def test_an_unknown_verification_is_refused_naming_it_and_the_values() -> None:
    assert problems(on="session_started", checks="gate", when={"verification": "missng"}) == (
        "triggers.x.when: missng isn't a value of verification; its values are configured,"
        " missing, off."
    )


def test_an_unknown_landing_review_is_refused_naming_its_two_values() -> None:
    assert problems(on="task_landed", checks="gate", when={"review": "nothing"}) == (
        "triggers.x.when: nothing isn't a value of review; its values are commit, batch."
    )


def test_a_landing_review_of_the_super_review_is_refused_though_reviews_finished_takes_it() -> None:
    assert problems(on="task_landed", checks="gate", when={"review": "super"}) == (
        "triggers.x.when: super isn't a value of review; its values are commit, batch."
    )
    check_triggers(
        {"x": Trigger(on="reviews_finished", checks="gate", when={"review": "super"})}, ROLES
    )


def test_an_unknown_set_status_is_refused_naming_its_two_values() -> None:
    assert problems(on="set_finished", checks="gate", when={"status": "maybe"}) == (
        "triggers.x.when: maybe isn't a value of status; its values are passed, failed."
    )


def test_a_set_finished_trigger_may_name_any_set_and_either_status() -> None:
    when: dict[str, str | tuple[str, ...]] = {
        "set": "renamed-long-ago",
        "status": ("passed", "failed"),
    }
    prompt = "{trigger}: {set} {status} at {commit}.\n\n{output}"
    check_triggers({"x": Trigger(on="set_finished", role="coder", prompt=prompt, when=when)}, ROLES)


def test_a_set_finished_trigger_using_another_events_variable_is_refused() -> None:
    assert problems(on="set_finished", role="coder", prompt="{diff}") == (
        "triggers.x.prompt: {diff} isn't a variable of set_finished; its variables are"
        " {trigger}, {set}, {status}, {commit}, {output}."
    )


@pytest.mark.parametrize("review", ["commit", "batch", ("commit", "batch")])
def test_each_landing_review_is_taken(review: str | tuple[str, ...]) -> None:
    check_triggers({"x": Trigger(on="task_landed", checks="gate", when={"review": review})}, ROLES)


@pytest.mark.parametrize("role", ["codr", ("coder", "codr")])
def test_an_unknown_role_in_when_is_refused_naming_it_and_the_roles(
    role: str | tuple[str, ...],
) -> None:
    assert problems(on="task_landed", checks="gate", when={"role": role}) == (
        "triggers.x.when: codr isn't a role; the roles are coder, reviewer."
    )


@pytest.mark.parametrize(
    "when",
    [
        {"verification": "configured"},
        {"verification": "missing"},
        {"verification": ("off", "missing")},
    ],
)
def test_each_verification_state_is_taken(when: dict[str, Any]) -> None:
    check_triggers({"x": Trigger(on="session_started", checks="gate", when=when)}, ROLES)


def test_the_roles_are_taken_in_when() -> None:
    when: dict[str, str | tuple[str, ...]] = {
        "role": ("coder", "reviewer"),
        "origin": "anyone",
        "status": "whatever",
    }
    check_triggers({"x": Trigger(on="task_finished", checks="gate", when=when)}, ROLES)


def test_a_step_in_when_is_the_projects_own_and_taken() -> None:
    trigger = Trigger(on="startup_check_failed", role="coder", prompt="Fix", when={"step": "Lint"})
    check_triggers({"x": trigger}, ROLES)


def test_each_unknown_value_in_when_is_refused_after_the_other_problems() -> None:
    trigger = Trigger(
        on="task_landed",
        role="nope",
        prompt="{nope}",
        when={"role": ("codr", "coder", "revewer"), "status": "done"},
    )
    with pytest.raises(ConfigError) as raised:
        check_triggers({"x": trigger}, ROLES)
    assert str(raised.value).splitlines() == [
        "triggers.x.role: nope isn't a role; the roles are coder, reviewer.",
        "triggers.x.prompt: {nope} isn't a variable of task_landed; its variables are {trigger},"
        " {commit}, {task}, {title}, {diff}, {missing}, {cap}, {queued}, {landed_since},"
        " {task_skills}.",
        "triggers.x.when: codr isn't a role; the roles are coder, reviewer.",
        "triggers.x.when: revewer isn't a role; the roles are coder, reviewer.",
        "triggers.x.when: status isn't a field of task_landed; its fields are role, origin,"
        " review.",
    ]


def test_a_variable_its_event_doesnt_give_is_refused_naming_it_and_the_variables() -> None:
    assert problems(on="task_finished", role="coder", prompt="Look at {diff} for {trigger}") == (
        "triggers.x.prompt: {diff} isn't a variable of task_finished; its variables are {trigger}."
    )


def test_each_unknown_variable_is_refused_once() -> None:
    message = problems(on="startup_check_failed", role="coder", prompt="{a} {b} {a} {step}")
    assert message.splitlines() == [
        "triggers.x.prompt: {a} isn't a variable of startup_check_failed; its variables are"
        " {trigger}, {step}, {output}.",
        "triggers.x.prompt: {b} isn't a variable of startup_check_failed; its variables are"
        " {trigger}, {step}, {output}.",
    ]


def test_every_needs_the_commits_event() -> None:
    assert problems(on="task_landed", role="coder", prompt="Go", every=5) == (
        'triggers.x.every: every = 5 needs on = "commits".'
    )


def test_a_manual_trigger_needs_a_label() -> None:
    assert problems(on="manual", role="coder", prompt="Go", manual=True) == (
        "triggers.x.label: manual = true needs a label."
    )
    assert problems(on="manual", role="coder", prompt="Go", manual=True, label="") == (
        "triggers.x.label: manual = true needs a label."
    )


def test_paths_need_the_task_landed_event() -> None:
    assert problems(on="commits", role="coder", prompt="Go", paths=("src/**",)) == (
        'triggers.x.paths: paths needs on = "task_landed".'
    )


def test_paths_name_at_least_one_glob() -> None:
    assert problems(on="task_landed", role="coder", prompt="Go", paths=()) == (
        "triggers.x.paths: a list of paths names at least one glob."
    )


def test_paths_on_a_landing_pass() -> None:
    landing = Trigger(on="task_landed", role="coder", prompt="Go", paths=("pyproject.toml",))
    check_triggers({"x": landing}, ROLES)
    assert landing.paths == ("pyproject.toml",)


def test_every_problem_of_every_trigger_is_named_on_a_line_of_its_own() -> None:
    triggers = {
        "a": Trigger(on="task_landed", role="nope", prompt="Go"),
        "b": Trigger(on="commits", role="coder", prompt="Go", manual=True),
    }
    with pytest.raises(ConfigError) as raised:
        check_triggers(triggers, ROLES)
    assert str(raised.value).splitlines() == [
        "triggers.a.role: nope isn't a role; the roles are coder, reviewer.",
        "triggers.b.label: manual = true needs a label.",
    ]


# review-rounds, which any number of triggers on task_landed take, whatever their roles


@pytest.mark.parametrize(
    "fields",
    [
        {"on": "task_finished", "role": "reviewer", "prompt": "Go"},
        {"on": "commits", "role": "coder", "prompt": "Go"},
        {"on": "task_finished", "role": "reviewer", "prompt": "Go", "enabled": False},
    ],
)
def test_review_rounds_off_a_landing_is_refused(fields: dict[str, Any]) -> None:
    problem = 'triggers.x.guards: review-rounds needs on = "task_landed".'
    assert problems(guards=("review-rounds",), **fields) == problem


def test_review_rounds_off_a_landing_is_refused_on_its_line_after_the_when() -> None:
    message = problems(
        on="task_finished",
        role="coder",
        prompt="Go",
        when={"step": "Lint"},
        guards=("review-rounds",),
        every=2,
    )
    assert message.splitlines() == [
        "triggers.x.when: step isn't a field of task_finished; its fields are role, origin,"
        " status.",
        'triggers.x.guards: review-rounds needs on = "task_landed".',
        'triggers.x.every: every = 2 needs on = "commits".',
    ]


def test_review_rounds_on_a_landing_takes_any_role() -> None:
    coder = REVIEW.model_copy(update={"role": "coder"})
    check_triggers({"x": coder}, ROLES)


def test_review_rounds_may_guard_several_enabled_triggers() -> None:
    coder = REVIEW.model_copy(update={"role": "coder"})
    check_triggers({"review": REVIEW, "check": CHECK, "again": REVIEW, "more": coder}, ROLES)


# raised-finding, which narrows only a batch's review, by the roles a trigger queues for

NEEDS_ROUNDS = 'triggers.x.guards: raised-finding needs review-rounds and on = "task_landed".'
NEEDS_ROLE = "triggers.x.guards: raised-finding needs a role; a trigger running a set has none."


def test_raised_finding_without_review_rounds_on_a_landing_is_refused() -> None:
    message = problems(on="task_landed", role="reviewer", prompt="Go", guards=("raised-finding",))
    assert message == NEEDS_ROUNDS


def test_raised_finding_off_a_landing_is_refused_besides_review_rounds() -> None:
    message = problems(
        on="task_finished", role="reviewer", prompt="Go", guards=("review-rounds", "raised-finding")
    )
    assert message.splitlines() == [
        'triggers.x.guards: review-rounds needs on = "task_landed".',
        NEEDS_ROUNDS,
    ]


def test_raised_finding_on_a_trigger_running_a_set_is_refused() -> None:
    trigger = Trigger(
        on="task_landed", checks="mutation", guards=("review-rounds", "raised-finding")
    )
    with pytest.raises(ConfigError) as raised:
        check_triggers({"x": trigger}, ROLES, sets=("mutation",))
    assert str(raised.value) == NEEDS_ROLE


def test_raised_finding_with_review_rounds_on_a_landing_role_trigger_loads() -> None:
    narrowed = REVIEW.model_copy(update={"guards": ("review-rounds", "raised-finding")})
    check_triggers({"x": narrowed}, ROLES)


# Merging the package's triggers with the project's


def test_without_a_projects_triggers_the_packages_are_merged_from_the_package() -> None:
    merged = merge_triggers(PACKAGE_TRIGGERS, PROJECT, {})
    assert {name: Trigger(**table) for name, table in merged.tables.items()} == PACKAGE_TRIGGERS
    assert merged.layers["review"]["on"] == PACKAGE
    assert merged.layers["review"]["guards"] == PACKAGE
    assert merged.layers["review"]["every"] == DEFAULT
    assert set(merged.layers["review"]) == set(Trigger.model_fields)


def test_a_project_overrides_only_the_fields_it_sets() -> None:
    merged = merge_triggers(PACKAGE_TRIGGERS, PROJECT, {"review": {"enabled": False}})
    assert Trigger(**merged.tables["review"]) == REVIEW.model_copy(update={"enabled": False})
    assert merged.layers["review"]["enabled"] == PROJECT
    assert merged.layers["review"]["prompt"] == PACKAGE


def test_a_project_adds_its_own_triggers_after_the_packages() -> None:
    nightly = {"on": "task_finished", "role": "coder", "prompt": "Tidy up"}
    merged = merge_triggers(PACKAGE_TRIGGERS, PROJECT, {"nightly": nightly, "check": {}})
    assert list(merged.tables) == ["review", "check", "nightly"]
    assert merged.tables["nightly"] == nightly
    assert merged.layers["nightly"]["role"] == PROJECT
    assert merged.layers["nightly"]["guards"] == DEFAULT


def test_what_isnt_a_table_is_left_as_it_is_for_validation_to_refuse() -> None:
    assert merge_triggers(PACKAGE_TRIGGERS, PROJECT, 5).tables == 5
    assert merge_triggers(PACKAGE_TRIGGERS, PROJECT, {"review": 5}).tables["review"] == 5


# Templates


def test_a_templates_variables_are_its_names_in_braces_each_once() -> None:
    assert template_variables("{a} and {b_c}, {a}; {Not} {not one} {}") == ("a", "b_c")


def test_rendering_puts_each_value_in_its_variables_place() -> None:
    assert render_template("Review {commit}: {title}", {"commit": "abc", "title": "T"}) == (
        "Review abc: T"
    )


def test_rendering_drops_a_paragraph_left_empty() -> None:
    template = "Title\n\n{missing}\n\nCap {cap}\n\n{queued}"
    assert render_template(template, {"missing": "", "cap": "5", "queued": " \n"}) == (
        "Title\n\nCap 5"
    )


def test_a_value_of_several_paragraphs_stays_whole() -> None:
    assert render_template("A\n\n{diff}", {"diff": "x\n\n\n\ny"}) == "A\n\nx\n\n\n\ny"


def test_braces_in_values_and_other_braces_are_left_as_they_are() -> None:
    assert render_template("{a} {B} {}", {"a": "{b}"}) == "{b} {B} {}"
