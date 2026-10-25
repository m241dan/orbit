"""Roles with ``enabled = false``: left out of the catalog with their triggers and lanes, queuing
no reviews, and refusing the work asked of them: messages, tasks and runs."""

import asyncio

from orbit.agent import ControlPlane, WorkspaceHost
from orbit.agent.domain.reviews import ReviewLimits
from orbit.agent.domain.roles import Role
from orbit.protocol import (
    BUILT_IN_ROLE_NAMES,
    CODER,
    CONFIGURATOR,
    CONFIGURE_CHECKS,
    NOT_CONFIGURED,
    PLANNER,
    REVIEWER,
    SUPER_REVIEW,
    SUPER_REVIEWER,
    UNKNOWN,
    AddTask,
    BusyChanged,
    Catalog,
    Command,
    CommandRefused,
    EditTask,
    Event,
    FireTrigger,
    ResetConversation,
    RunTasks,
    SendMessage,
    StartupChecked,
    TasksChanged,
    TaskSpec,
)
from orbit.tui.view.triggers import trigger_key_labels
from tests.agent_fakes import (
    HELLO,
    ROLES,
    Configurator,
    FakeTree,
    Steps,
    agent_exited,
    board,
    board_of,
    checked,
    claims_done,
    events_until,
    of_type,
    put_on_board,
    refused,
    send,
    session_for,
    settle,
    start,
    suggests,
    until_idle,
    verified,
)
from tests.connection_helpers import say, taken
from tests.plane_fakes import payloads

CODE = TaskSpec(prompt="Add a flag.")


def disabling(*names: str) -> dict[str, Role]:
    """The built-in roles, with those ``names`` disabled."""
    return {
        name: role.model_copy(update={"enabled": name not in names}) for name, role in ROLES.items()
    }


def disabled(role: str) -> CommandRefused:
    """The refusal of work in ``role``, which is disabled."""
    return refused(NOT_CONFIGURED, f"{role} is disabled in this project's configuration.")


async def catalog_of(host: WorkspaceHost) -> Catalog:
    """The catalog a connection to a control plane serving ``host`` gets after its Hello."""
    plane = ControlPlane([host])
    connection = plane.connect()
    await say(connection, "hello", HELLO)
    _, listed = payloads(taken(connection))
    await plane.close()
    assert isinstance(listed, Catalog)
    return listed


async def refusal_of(session: WorkspaceHost, command: Command) -> list[Event]:
    """The events until ``command``, sent to ``session``, is refused."""
    await send(session, command)
    return await events_until(session, of_type(CommandRefused))


async def test_with_every_role_enabled_the_catalog_lists_them_their_triggers_and_lanes() -> None:
    session, _, _ = session_for(tree=FakeTree(), review=True)
    listed = await catalog_of(session)
    assert tuple(agent.name for agent in listed.agents) == BUILT_IN_ROLE_NAMES
    assert [trigger.name for trigger in listed.triggers] == [SUPER_REVIEW, CONFIGURE_CHECKS]
    assert listed.lanes == ("work", "planner", "review")


async def test_a_disabled_super_reviewer_leaves_the_catalog_with_its_trigger_and_key() -> None:
    session, _, _ = session_for(tree=FakeTree(), review=True, roles=disabling(SUPER_REVIEWER))
    listed = await catalog_of(session)
    assert [agent.name for agent in listed.agents] == [PLANNER, CODER, REVIEWER, CONFIGURATOR]
    assert [trigger.name for trigger in listed.triggers] == [CONFIGURE_CHECKS]
    assert trigger_key_labels(listed.triggers) == {"ctrl+g": None, "ctrl+e": "Set up checks"}
    assert listed.lanes == ("work", "planner", "review")


async def test_a_disabled_configurator_takes_its_trigger_and_key_out_of_the_catalog() -> None:
    session, _, _ = session_for(roles=disabling(CONFIGURATOR))
    listed = await catalog_of(session)
    assert [trigger.name for trigger in listed.triggers] == [SUPER_REVIEW]
    assert trigger_key_labels(listed.triggers) == {"ctrl+g": "Super review", "ctrl+e": None}


async def test_disabled_reviewers_and_planner_take_their_lanes_out_of_the_catalog() -> None:
    roles = disabling(PLANNER, REVIEWER, SUPER_REVIEWER)
    session, _, _ = session_for(tree=FakeTree(), review=True, roles=roles)
    listed = await catalog_of(session)
    assert [agent.name for agent in listed.agents] == [CODER, CONFIGURATOR]
    assert listed.lanes == ("work",)


async def test_firing_a_disabled_super_reviewers_trigger_is_refused_as_unknown() -> None:
    session, _, _ = session_for(tree=FakeTree(), review=True, roles=disabling(SUPER_REVIEWER))
    events = await refusal_of(session, FireTrigger(trigger=SUPER_REVIEW))
    await session.close()
    message = "No trigger 'super-review'; the triggers are configure-checks."
    assert events[-1] == refused(UNKNOWN, message)
    assert not any(isinstance(event, TasksChanged) for event in events)


async def test_firing_a_disabled_configurators_trigger_is_refused_as_unknown() -> None:
    session, _, _ = session_for(verification=verified(Steps()), roles=disabling(CONFIGURATOR))
    events = await refusal_of(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    await session.close()
    message = "No trigger 'configure-checks'; the triggers are super-review."
    assert events[-1] == refused(UNKNOWN, message)


async def test_with_both_triggers_roles_disabled_a_trigger_is_refused_as_there_are_none() -> None:
    session, _, _ = session_for(roles=disabling(SUPER_REVIEWER, CONFIGURATOR))
    events = await refusal_of(session, FireTrigger(trigger=SUPER_REVIEW))
    await session.close()
    assert events[-1] == refused(UNKNOWN, "No trigger 'super-review'; there are none.")


async def test_a_disabled_reviewer_queues_no_review_as_work_lands() -> None:
    session, _, launcher = session_for(
        claims_done,
        reviewers=(suggests(),),
        tree=FakeTree(),
        review=True,
        roles=disabling(REVIEWER),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()
    assert [(t.task.role, t.status) for t in board(events)] == [(CODER, "completed")]
    assert [launch.agent.role for launch in launcher.launches] == [CODER]


async def test_a_disabled_super_reviewer_queues_no_super_review_however_many_commits_land() -> None:
    session, _, _ = session_for(
        claims_done,
        reviewers=(suggests(),),
        tree=FakeTree(),
        review=True,
        review_limits=ReviewLimits(super_every=1),
        roles=disabling(SUPER_REVIEWER),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()
    assert [t.task.role for t in board(events)] == [CODER, REVIEWER]


async def test_a_message_to_a_disabled_planner_is_refused_freeing_its_lane() -> None:
    session, _, launcher = session_for(roles=disabling(PLANNER))
    events = await refusal_of(session, SendMessage(agent=PLANNER, text="Plan it."))
    await session.close()
    assert events[-1] == disabled(PLANNER)
    assert BusyChanged(busy=False, lane="planner") in events
    assert launcher.launches == []


async def test_resetting_a_disabled_planners_conversation_is_refused() -> None:
    session, _, _ = session_for(roles=disabling(PLANNER))
    events = await refusal_of(session, ResetConversation(agent=PLANNER))
    await session.close()
    assert events[-1] == disabled(PLANNER)


async def test_adding_a_task_in_a_disabled_role_is_refused_naming_it() -> None:
    session, _, _ = session_for(roles=disabling(CODER))
    events = await refusal_of(session, AddTask(task=CODE))
    await session.close()
    assert events[-1] == disabled(CODER)
    assert board_of(session).tasks == ()


async def test_editing_a_task_into_a_disabled_role_is_refused_naming_it() -> None:
    session, _, _ = session_for(roles=disabling(CODER))
    put_on_board(session, TaskSpec(prompt="Set up checks.", role=CONFIGURATOR))
    events = await refusal_of(session, EditTask(task_id="1", task=CODE))
    await session.close()
    assert events[-1] == disabled(CODER)
    assert [state.task.role for state in board_of(session).tasks] == [CONFIGURATOR]


async def test_running_tasks_with_one_pending_in_a_disabled_role_is_refused_naming_it() -> None:
    session, _, launcher = session_for(claims_done, roles=disabling(CODER))
    put_on_board(session, CODE)
    events = await refusal_of(session, RunTasks())
    await session.close()
    assert events[-1] == disabled(CODER)
    assert [state.status for state in board_of(session).tasks] == ["pending"]
    assert launcher.launches == []


async def test_a_disabled_roles_task_is_left_on_the_board_by_a_run_it_does_not_block() -> None:
    session, _, launcher = session_for(
        Configurator(),
        claims_done,
        verification=verified(Steps(True)),
        tree=FakeTree(),
        roles=disabling(CODER),
    )
    put_on_board(session, CODE)
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    events = await until_idle(session)
    await session.close()
    shown = [(t.task.role, t.status) for t in board(events)]
    assert shown == [(CONFIGURATOR, "unverified"), (CODER, "pending")]
    assert [launch.agent.role for launch in launcher.launches] == [CONFIGURATOR]


async def test_a_failing_startup_check_adds_no_draft_while_the_coder_is_disabled() -> None:
    session, _, _ = session_for(
        verification=checked(Steps(False)),
        tree=FakeTree(),
        check_at_startup=True,
        roles=disabling(CODER),
    )
    events = await events_until(session, agent_exited("startup-check"))
    await session.close()
    assert StartupChecked(passed=False, step="Lint", output="1 test failed") in events
    assert not any(isinstance(event, TasksChanged) for event in events)
    assert board_of(session).tasks == ()
