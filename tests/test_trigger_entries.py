"""The catalog's trigger entries: a workspace's enabled triggers with their tabs and roles, the
built-in ones the user fires, as their configured triggers would fire by hand, and each trigger
listed once across the workspaces served."""

from orbit.agent.domain.catalog import TRIGGERS, catalog, trigger_entries, trigger_names
from orbit.agent.domain.roles import Role
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import (
    CONFIGURATOR,
    CONFIGURE_CHECKS,
    DRIFT_REVIEWER,
    SUPER_LENS_REVIEWERS,
    SUPER_REVIEW,
    TriggerEntry,
)
from tests.agent_fakes import ROLES, SHIPPED_TRIGGERS

TIDY = Trigger(on="task_finished", role="coder", prompt="Tidy up", tab="tidying")
"""A project's trigger whose agents share the ``tidying`` tab."""

ARCH = "arch-reviewer"
"""A project's own reviewer, which a project may give the super review instead of its lenses."""

ARCH_SUPER = SHIPPED_TRIGGERS[SUPER_REVIEW].model_copy(update={"role": ARCH})
"""The shipped super review overridden to queue a task for :data:`ARCH` alone."""


def roles_with(*disabled: str, arch: bool = True) -> dict[str, Role]:
    """The built-in roles and :data:`ARCH`, enabled if ``arch``, with those ``disabled``."""
    roles = {**ROLES, ARCH: ROLES[DRIFT_REVIEWER].model_copy(update={"enabled": arch})}
    return {
        name: role.model_copy(update={"enabled": role.enabled and name not in disabled})
        for name, role in roles.items()
    }


def test_each_enabled_trigger_is_listed_with_its_tab_and_roles() -> None:
    entries = trigger_entries({"tidy": TIDY}, ROLES)
    assert entries[0] == TriggerEntry(
        name="tidy", label="tidy", manual=False, tab="tidying", roles=("coder",)
    )


def test_a_built_in_trigger_left_unconfigured_is_not_offered() -> None:
    assert [entry.name for entry in trigger_entries({"tidy": TIDY}, ROLES)] == ["tidy"]
    assert trigger_names({"tidy": TIDY}, ROLES) == ()


def test_a_built_in_trigger_running_a_set_follows_the_workspaces_own_as_the_built_in() -> None:
    checks = Trigger(on="manual", checks="slow")
    entries = trigger_entries({"tidy": TIDY, SUPER_REVIEW: checks}, ROLES)
    assert entries[1:] == TRIGGERS[:1]


def test_a_shipped_built_in_trigger_is_manual_and_keeps_its_tab() -> None:
    entries = {entry.name: entry for entry in trigger_entries(SHIPPED_TRIGGERS, ROLES)}
    assert (entries[SUPER_REVIEW].manual, entries[SUPER_REVIEW].tab) == (True, "super-review")
    assert (entries[CONFIGURE_CHECKS].manual, entries[CONFIGURE_CHECKS].tab) == (True, None)
    assert entries["bugs-review"].manual is False


def test_disabled_triggers_and_those_running_checks_are_left_out() -> None:
    off = TIDY.model_copy(update={"enabled": False})
    checks = Trigger(on="session_started", checks="task-checks")
    assert trigger_entries({"tidy": off, "check": checks}, ROLES) == ()


def test_a_trigger_named_in_two_workspaces_is_listed_as_the_first_has_it() -> None:
    other = TriggerEntry(name="tidy", label="tidy", manual=False)
    listed = catalog((), (), (*trigger_entries({"tidy": TIDY}, ROLES), other), (), ())
    assert [(entry.name, entry.tab) for entry in listed.triggers] == [("tidy", "tidying")]


def test_the_shipped_built_in_triggers_are_offered_with_their_roles_enabled() -> None:
    assert trigger_names(SHIPPED_TRIGGERS, ROLES) == (SUPER_REVIEW, CONFIGURE_CHECKS)


def test_the_super_review_is_offered_while_any_of_its_lenses_is_enabled() -> None:
    names = trigger_names(SHIPPED_TRIGGERS, roles_with(*SUPER_LENS_REVIEWERS[1:]))
    assert names == (SUPER_REVIEW, CONFIGURE_CHECKS)


def test_the_super_review_is_not_offered_with_all_its_lenses_disabled() -> None:
    names = trigger_names(SHIPPED_TRIGGERS, roles_with(*SUPER_LENS_REVIEWERS))
    assert names == (CONFIGURE_CHECKS,)


def test_set_up_checks_is_not_offered_with_the_configurator_disabled() -> None:
    assert trigger_names(SHIPPED_TRIGGERS, roles_with(CONFIGURATOR)) == (SUPER_REVIEW,)


def test_a_super_review_overridden_to_an_enabled_role_is_offered_without_its_lenses() -> None:
    triggers = {**SHIPPED_TRIGGERS, SUPER_REVIEW: ARCH_SUPER}
    roles = roles_with(*SUPER_LENS_REVIEWERS)
    assert trigger_names(triggers, roles) == (SUPER_REVIEW, CONFIGURE_CHECKS)
    entries = {entry.name: entry for entry in trigger_entries(triggers, roles)}
    assert (entries[SUPER_REVIEW].manual, entries[SUPER_REVIEW].roles) == (True, (ARCH,))


def test_a_super_review_overridden_to_a_disabled_role_is_not_offered_beside_its_lenses() -> None:
    triggers = {**SHIPPED_TRIGGERS, SUPER_REVIEW: ARCH_SUPER}
    roles = roles_with(arch=False)
    assert trigger_names(triggers, roles) == (CONFIGURE_CHECKS,)
    assert SUPER_REVIEW not in [entry.name for entry in trigger_entries(triggers, roles)]


def test_a_disabled_built_in_trigger_is_not_offered() -> None:
    off = SHIPPED_TRIGGERS[SUPER_REVIEW].model_copy(update={"enabled": False})
    assert trigger_names({**SHIPPED_TRIGGERS, SUPER_REVIEW: off}, ROLES) == (CONFIGURE_CHECKS,)
