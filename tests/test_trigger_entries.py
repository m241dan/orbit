"""The catalog's trigger entries: a workspace's enabled triggers with their tabs and roles, the
built-in ones the user fires, and each trigger listed once across the workspaces served."""

from orbit.agent.domain.catalog import TRIGGERS, catalog, trigger_entries
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import CONFIGURE_CHECKS, SUPER_REVIEW, TriggerEntry
from tests.agent_fakes import ROLES, SHIPPED_TRIGGERS

TIDY = Trigger(on="task_finished", role="coder", prompt="Tidy up", tab="tidying")
"""A project's trigger whose agents share the ``tidying`` tab."""


def test_each_enabled_trigger_is_listed_with_its_tab_and_roles() -> None:
    entries = trigger_entries({"tidy": TIDY}, ROLES)
    assert entries[0] == TriggerEntry(
        name="tidy", label="tidy", manual=False, tab="tidying", roles=("coder",)
    )


def test_the_built_in_triggers_the_user_fires_follow_the_workspaces_own() -> None:
    entries = trigger_entries({"tidy": TIDY}, ROLES)
    assert entries[1:] == TRIGGERS


def test_a_shipped_built_in_trigger_is_manual_and_keeps_its_tab() -> None:
    entries = {entry.name: entry for entry in trigger_entries(SHIPPED_TRIGGERS, ROLES)}
    assert (entries[SUPER_REVIEW].manual, entries[SUPER_REVIEW].tab) == (True, "super-review")
    assert (entries[CONFIGURE_CHECKS].manual, entries[CONFIGURE_CHECKS].tab) == (True, None)
    assert entries["bugs-review"].manual is False


def test_disabled_triggers_and_those_running_checks_are_left_out() -> None:
    off = TIDY.model_copy(update={"enabled": False})
    checks = Trigger(on="session_started", checks="task-checks")
    assert trigger_entries({"tidy": off, "check": checks}, ROLES) == TRIGGERS


def test_a_trigger_named_in_two_workspaces_is_listed_as_the_first_has_it() -> None:
    other = TriggerEntry(name="tidy", label="tidy", manual=False)
    listed = catalog((), (), (*trigger_entries({"tidy": TIDY}, ROLES), other), (), ())
    assert [(entry.name, entry.tab) for entry in listed.triggers] == [
        ("tidy", "tidying"),
        (SUPER_REVIEW, None),
        (CONFIGURE_CHECKS, None),
    ]
