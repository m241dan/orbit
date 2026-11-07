"""Skills delivered in agents' instructions: an agent, a task's or a planner turn's, on any
runtime, is launched with its role's instructions and then the skills it was given, its role's,
its task's and the project's mapped ones, and its process's span records those skills and the
files of them it read. An agent that proposes tasks is also told the skills it can give them, and
the tasks it proposes carry those it names. The delivery is also tested on its own, without a
desk."""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan

from orbit.agent.application.agent_trace import PROCESS_SPAN, AgentProcess
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.ports import AgentLaunch, Reply
from orbit.agent.application.skill_delivery import SkillDelivery
from orbit.agent.domain.layers import PACKAGE
from orbit.agent.domain.skills import Skill, SkillFile, SkillLibrary, SkillMapping
from orbit.agent.domain.tools import READ_SKILL
from orbit.agent.external import claude_cli, kiro_cli
from orbit.agent.external.kiro_agents import kiro_agent_config
from orbit.protocol import (
    CODER,
    PLANNER,
    ActivateSession,
    ConversationReset,
    Event,
    ResetConversation,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import (
    ROLES,
    Call,
    Script,
    agents_for,
    board_of,
    chats,
    claims_done,
    events_until,
    of_type,
    plan,
    run,
    send,
    session_for,
)
from tests.session_builders import on_claude, opened, saved, store_with
from tests.telemetry_helpers import Traced


def skill(name: str) -> Skill:
    """The skill ``name``, whose body names it, with its SKILL.md and a ``testing.md``, each
    holding its skill's name and its path."""
    files = {
        path: SkillFile(text=f"{name}'s {path}", layer=PACKAGE, location=f"/s/{name}/{path}")
        for path in ("SKILL.md", "testing.md")
    }
    return Skill(name=name, description=f"{name}.", body=f"Be {name}.\n", files=files)


LIBRARY = SkillLibrary(
    skills={
        name: skill(name) for name in ("application", "code-style", "layers", "python", "security")
    }
)
"""The skills a session's agents can be given."""

CODE = (SkillMapping(paths=("src", "tests"), use=("python", "code-style")),)
"""A project mapping its ``src`` and ``tests`` to ``python`` and ``code-style``."""

READING = (
    'A line such as "Read `code-style`\'s `testing.md`." means: call read_skill with skill'
    " `code-style` and file `testing.md`."
)
"""The line telling an agent how to read the files its skills point it to."""

CODER_SKILLS = (
    f"## Skills\n\n{READING}\n\n"
    "### code-style\n\nBe code-style.\n\n"
    "### security\n\nBe security.\n\n"
    "### python\n\nBe python.\n\n"
    "`python`, `code-style` apply under `src`, `tests`."
)
"""What a coder, with its built-in skills, is given under :data:`CODE`."""


async def coder_launch(
    *scripts: Script, task: TaskSpec | None = None, **options: Any
) -> AgentLaunch:
    """How a coder playing ``scripts``, or :func:`claims_done`, is launched for ``task``, or a
    task naming no skills, in a session given ``options``, such as ``skill_library``."""
    session, _, launcher = session_for(*(scripts or (claims_done,)), **options)
    await asyncio.wait_for(run(session, *(() if task is None else (task,))), 5)
    await session.close()
    [launch] = launcher.launches
    return launch


CHOSEN = TaskSpec(prompt="Add a flag.", skills=("python", "application"))
"""A coder's task naming the skills ``python`` and ``application``."""

MAPPED = (SkillMapping(paths=("src", "tests"), use=("python", "layers", "code-style")),)
"""A project mapping its ``src`` and ``tests`` to ``python``, ``layers`` and ``code-style``."""

CATALOGUE = (
    "## Skills you can give tasks\n\n"
    "application: application.\n"
    "code-style: code-style.\n"
    "layers: layers.\n"
    "python: python.\n"
    "security: security."
)
"""What an agent that proposes tasks is told of the skills in :data:`LIBRARY` it can give them."""


async def test_a_coder_is_launched_with_its_instructions_then_its_skills() -> None:
    launch = await coder_launch(skill_library=LIBRARY, skill_mappings=CODE)
    assert launch.instructions == f"You are orbit's coder.\n\n{CODER_SKILLS}"


async def test_a_role_with_no_skills_or_mapping_is_launched_with_its_instructions_alone() -> None:
    roles = {**ROLES, CODER: ROLES[CODER].model_copy(update={"skills": ()})}
    launch = await coder_launch(skill_library=LIBRARY, roles=roles)
    assert launch.instructions == "You are orbit's coder."


async def test_both_runtimes_tell_an_agent_its_skills_after_orbits_rules() -> None:
    launch = await coder_launch(skill_library=LIBRARY, skill_mappings=CODE)
    argv = claude_cli.agent_args(launch)
    told = f"\n\nYou are orbit's coder.\n\n{CODER_SKILLS}"
    assert argv[argv.index("--append-system-prompt") + 1] == f"{claude_cli.AGENT_RULES}{told}"
    config = kiro_agent_config(kiro_cli.AGENT_RULES, launch)
    assert config["prompt"] == f"{kiro_cli.AGENT_RULES}{told}"


async def test_a_task_naming_skills_gets_its_roles_then_its_own_then_the_mapped_ones() -> None:
    launch = await coder_launch(task=CHOSEN, skill_library=LIBRARY, skill_mappings=MAPPED)
    assert launch.instructions == (
        f"You are orbit's coder.\n\n## Skills\n\n{READING}\n\n"
        "### code-style\n\nBe code-style.\n\n"
        "### security\n\nBe security.\n\n"
        "### python\n\nBe python.\n\n"
        "### application\n\nBe application.\n\n"
        "### layers\n\nBe layers.\n\n"
        "`python`, `layers`, `code-style` apply under `src`, `tests`."
    )


async def test_a_skill_both_the_role_and_the_task_name_is_delivered_once() -> None:
    task = TaskSpec(prompt="Add a flag.", skills=("security", "python"))
    launch = await coder_launch(task=task, skill_library=LIBRARY, skill_mappings=MAPPED)
    assert launch.instructions == (
        f"You are orbit's coder.\n\n## Skills\n\n{READING}\n\n"
        "### code-style\n\nBe code-style.\n\n"
        "### security\n\nBe security.\n\n"
        "### python\n\nBe python.\n\n"
        "### layers\n\nBe layers.\n\n"
        "`python`, `layers`, `code-style` apply under `src`, `tests`."
    )


async def test_a_planner_turn_carries_the_planners_skills_and_the_workspaces_mapped_ones() -> None:
    session, _, launcher = session_for(skill_library=LIBRARY, skill_mappings=CODE)
    launcher.planner_turns = [chats]
    await plan(session, "Plan.")
    await session.close()
    [launch] = launcher.launches
    assert launch.instructions == (
        f"You are orbit's planner.\n\n## Skills\n\n{READING}\n\n"
        "### code-style\n\nBe code-style.\n\n"
        "### python\n\nBe python.\n\n"
        "`python`, `code-style` apply under `src`, `tests`.\n\n"
        f"{CATALOGUE}"
    )


async def test_a_planner_turn_after_the_workspace_reopens_carries_its_changed_mapping() -> None:
    store = store_with(saved("auth", conversations={PLANNER: on_claude("talk-9")}))
    changed = (SkillMapping(paths=("src",), use=("security",)),)
    session, _, launcher = session_for(store=store, skill_library=LIBRARY, skill_mappings=changed)
    await opened(session, ActivateSession(session="auth"))
    launcher.planner_turns = [chats]
    await plan(session, "Where were we?")
    await session.close()
    assert launcher.resumed == ["talk-9"]
    assert launcher.launches[-1].instructions == (
        f"You are orbit's planner.\n\n## Skills\n\n{READING}\n\n"
        "### code-style\n\nBe code-style.\n\n"
        "### security\n\nBe security.\n\n"
        "`security` applies under `src`.\n\n"
        f"{CATALOGUE}"
    )


async def test_a_planner_turn_after_a_reset_carries_the_skills_its_role_has_then() -> None:
    planner = ROLES[PLANNER].model_copy(update={"skills": ("security",)})
    session, _, launcher = session_for(skill_library=LIBRARY, planner_role=lambda: planner)
    launcher.planner_turns = [chats, chats]
    await plan(session, "Plan.")
    await send(session, ResetConversation(agent=PLANNER))
    await events_until(session, of_type(ConversationReset))
    await plan(session, "Plan again.")
    await session.close()
    before, after = (launch.instructions for launch in launcher.launches)
    assert before.endswith(f"### code-style\n\nBe code-style.\n\n{CATALOGUE}")
    assert after == (
        f"You are orbit's planner.\n\n## Skills\n\n{READING}\n\n### security\n\nBe security."
        f"\n\n{CATALOGUE}"
    )


# The skills a proposing agent can give tasks


async def planner_launch(**options: Any) -> AgentLaunch:
    """How the planner is launched for a turn in a session given ``options``, such as
    ``skill_library``."""
    session, _, launcher = session_for(**options)
    launcher.planner_turns = [chats]
    await plan(session, "Plan.")
    await session.close()
    [launch] = launcher.launches
    return launch


async def test_a_planner_given_no_skills_is_told_the_skills_it_can_give_in_their_place() -> None:
    roles = {**ROLES, PLANNER: ROLES[PLANNER].model_copy(update={"skills": ()})}
    launch = await planner_launch(skill_library=LIBRARY, roles=roles)
    assert launch.instructions == f"You are orbit's planner.\n\n{CATALOGUE}"


async def test_a_coder_isnt_told_the_skills_it_could_give_tasks() -> None:
    launch = await coder_launch(skill_library=LIBRARY, skill_mappings=CODE)
    assert "## Skills you can give tasks" not in launch.instructions


async def test_a_planner_in_a_session_without_skills_is_told_its_instructions_alone() -> None:
    roles = {**ROLES, PLANNER: ROLES[PLANNER].model_copy(update={"skills": ()})}
    launch = await planner_launch(roles=roles)
    assert launch.instructions == "You are orbit's planner."


async def proposing(*lists: list[str] | None) -> tuple[list[Reply], list[tuple[str, ...]]]:
    """The planner's replies to proposing a task naming each of ``lists`` of skills, in a session
    given :data:`LIBRARY`, and the skills of the drafts it added, in list order."""
    replies: list[Reply] = []

    async def proposes(call: Call) -> TurnEndReason:
        for skills in lists:
            replies.append(await call("propose_task", title="T", prompt="P", skills=skills))
        return "completed"

    session, _, launcher = session_for(skill_library=LIBRARY)
    launcher.planner_turns = [proposes]
    await plan(session, "Plan.")
    drafts = board_of(session).drafts_of(PLANNER)
    await session.close()
    return replies, [draft.task.skills for draft in drafts]


async def test_a_proposed_task_carries_the_skills_the_planner_names_in_order() -> None:
    replies, skills = await proposing(["python", "application"])
    assert replies == [Reply("Added draft task 1; the user will accept or drop it.")]
    assert skills == [("python", "application")]


async def test_a_skill_named_twice_in_a_proposal_is_carried_once() -> None:
    _, skills = await proposing(["python", "python"])
    assert skills == [("python",)]


async def test_a_proposal_naming_no_skills_carries_none() -> None:
    _, skills = await proposing(None, [])
    assert skills == [(), ()]


async def test_a_proposal_naming_an_unknown_skill_is_refused_and_adds_no_draft() -> None:
    replies, skills = await proposing(["python", "pyton"])
    assert replies == [
        Reply(
            "Task skills names pyton, which isn't a skill; the skills are application,"
            " code-style, layers, python, security.",
            is_error=True,
        )
    ]
    assert skills == []


# The process's span


def attributes(span: ReadableSpan) -> Mapping[str, Any]:
    assert span.attributes is not None
    return span.attributes


async def reads(call: Call) -> TurnEndReason:
    """Reads code-style's ``testing.md`` twice, security's SKILL.md, then a file code-style
    lacks and a skill it wasn't given, which are refused."""
    await call("get_task")
    for name, path in (
        ("code-style", "testing.md"),
        ("security", "SKILL.md"),
        ("code-style", "testing.md"),
        ("code-style", "nope.md"),
        ("cpp", "SKILL.md"),
    ):
        await call("read_skill", skill=name, file=path)
    await call("complete_task", summary="Read.")
    return "completed"


async def test_the_process_records_the_skills_delivered_and_each_file_read_once_sorted() -> None:
    traced = Traced()
    await coder_launch(
        reads, skill_library=LIBRARY, skill_mappings=CODE, telemetry=traced.telemetry
    )
    [process] = traced.spans(PROCESS_SPAN)
    assert attributes(process)["orbit.agent.skills"] == ("code-style", "security", "python")
    assert attributes(process)["orbit.agent.skill_files"] == (
        "code-style/testing.md",
        "security/SKILL.md",
    )


async def test_a_task_naming_skills_reads_those_delivered_mapped_ones_included() -> None:
    replies: list[Reply] = []
    names = ("code-style", "security", "python", "application", "layers")

    async def reads_each(call: Call) -> TurnEndReason:
        await call("get_task")
        for name in names:
            replies.append(await call("read_skill", skill=name, file="SKILL.md"))
        await call("complete_task", summary="Read.")
        return "completed"

    await coder_launch(reads_each, task=CHOSEN, skill_library=LIBRARY, skill_mappings=MAPPED)
    assert replies == [Reply(f"{name}'s SKILL.md") for name in names]


async def test_the_process_of_a_task_naming_skills_records_those_delivered() -> None:
    traced = Traced()
    await coder_launch(
        task=CHOSEN, skill_library=LIBRARY, skill_mappings=MAPPED, telemetry=traced.telemetry
    )
    [process] = traced.spans(PROCESS_SPAN)
    assert attributes(process)["orbit.agent.skills"] == (
        "code-style",
        "security",
        "python",
        "application",
        "layers",
    )


async def test_a_process_given_no_skills_and_reading_none_records_none() -> None:
    traced = Traced()
    await coder_launch(telemetry=traced.telemetry)
    [process] = traced.spans(PROCESS_SPAN)
    assert attributes(process)["orbit.agent.skills"] == ()
    assert attributes(process)["orbit.agent.skill_files"] == ()


async def test_a_planner_turns_process_records_its_skills_and_the_files_it_read() -> None:
    async def turn(call: Call) -> TurnEndReason:
        await call("read_skill", skill="python", file="testing.md")
        return "completed"

    traced = Traced()
    session, _, launcher = session_for(
        skill_library=LIBRARY, skill_mappings=CODE, telemetry=traced.telemetry
    )
    launcher.planner_turns = [turn]
    await plan(session, "Plan.")
    await session.close()
    [process] = traced.spans(PROCESS_SPAN)
    assert attributes(process)["orbit.agent.skills"] == ("code-style", "python")
    assert attributes(process)["orbit.agent.skill_files"] == ("python/testing.md",)


async def ignored(event: Event) -> None:
    pass


SECURITY = {"skill": "security", "file": "SKILL.md"}
"""The arguments of a call reading security's SKILL.md."""


async def test_a_read_from_an_agent_whose_process_isnt_traced_is_still_answered() -> None:
    agents, _ = agents_for(skill_library=LIBRARY)
    desk = AgentDesk(agents, ignored)

    class Reader:
        async def handle(self, tool: str, arguments: dict[str, Any]) -> Reply:
            return await desk.tools(CODER, agent.id)[tool](arguments)

    agent = desk.open(Reader(), CODER, "1")
    assert await desk.handle(agent.token, READ_SKILL, SECURITY) == Reply("security's SKILL.md")


async def test_a_read_for_an_agent_not_yet_opened_is_still_answered() -> None:
    agents, _ = agents_for(skill_library=LIBRARY)
    read_skill = AgentDesk(agents, ignored).tools(CODER, None)[READ_SKILL]
    assert await read_skill(SECURITY) == Reply("security's SKILL.md")


# Delivery on its own, without a desk


def delivering(process: AgentProcess | None = None) -> SkillDelivery:
    """Delivery of :data:`LIBRARY`'s skills under :data:`MAPPED`, noting agent ``agent-1``'s reads
    on ``process``, if there's one, and no other agent's."""
    return SkillDelivery(LIBRARY, MAPPED, {"agent-1": process}.get)


MAPPED_TO_CODER = ("code-style", "security", "python", "layers")
"""The skills a coder is delivered under :data:`MAPPED` for a task naming none."""


def test_delivery_tells_an_agent_its_roles_instructions_then_its_roles_and_tasks_skills() -> None:
    skills = delivering()
    skills.open("agent-1", ("python", "application"))
    assert skills.instructions(ROLES[CODER], "agent-1") == (
        f"You are orbit's coder.\n\n## Skills\n\n{READING}\n\n"
        "### code-style\n\nBe code-style.\n\n"
        "### security\n\nBe security.\n\n"
        "### python\n\nBe python.\n\n"
        "### application\n\nBe application.\n\n"
        "### layers\n\nBe layers.\n\n"
        "`python`, `layers`, `code-style` apply under `src`, `tests`."
    )


def test_delivery_chooses_a_tasks_skills_after_its_roles_then_the_mapped_ones_each_once() -> None:
    skills = delivering()
    skills.open("agent-1", ("security", "application"))
    assert skills.delivered(ROLES[CODER], "agent-1") == (
        "code-style",
        "security",
        "application",
        "python",
        "layers",
    )


@pytest.mark.parametrize("agent_id", ["agent-1", None])
def test_delivery_gives_a_task_naming_no_skills_or_no_agent_yet_the_mapped_ones(
    agent_id: str | None,
) -> None:
    skills = delivering()
    skills.open("agent-1", ())
    assert skills.delivered(ROLES[CODER], agent_id) == MAPPED_TO_CODER


def test_delivery_forgets_a_closed_agents_task_skills() -> None:
    skills = delivering()
    skills.open("agent-1", ("application",))
    skills.close("agent-1")
    assert skills.delivered(ROLES[CODER], "agent-1") == MAPPED_TO_CODER


def test_delivery_tells_a_role_that_proposes_tasks_the_skills_it_can_give_them() -> None:
    planner = ROLES[PLANNER].model_copy(update={"skills": ()})
    skills = SkillDelivery(LIBRARY, (), lambda agent_id: None)
    assert skills.instructions(planner, None) == f"You are orbit's planner.\n\n{CATALOGUE}"


async def test_read_skill_answers_with_the_file_of_a_skill_given() -> None:
    read_skill = delivering().tool(lambda: ROLES[CODER], "agent-1")
    assert await read_skill({"skill": "python", "file": "testing.md"}) == Reply(
        "python's testing.md"
    )


async def test_read_skill_refuses_a_skill_not_given_listing_those_given() -> None:
    skills = delivering()
    skills.open("agent-1", ("python",))
    read_skill = skills.tool(lambda: ROLES[CODER], "agent-1")
    assert await read_skill({"skill": "application", "file": "SKILL.md"}) == Reply(
        "You weren't given the skill application; your skills are code-style, security, python,"
        " layers.",
        is_error=True,
    )


ORBITS = (SkillMapping(paths=("src", "tests"), use=("application", "python", "layers", "orbit")),)
"""A project mapping its ``src`` and ``tests`` as orbit's own does."""


def orbits_delivery() -> SkillDelivery:
    """Delivery under :data:`ORBITS` of :data:`LIBRARY`'s skills and ``orbit``, with agent
    ``agent-1`` opened for a task naming ``python``."""
    library = SkillLibrary(skills={**LIBRARY.skills, "orbit": skill("orbit")})
    skills = SkillDelivery(library, ORBITS, lambda agent_id: None)
    skills.open("agent-1", ("python",))
    return skills


def test_a_task_naming_a_mapped_skill_is_told_the_other_mapped_ones_and_where_they_apply() -> None:
    told = orbits_delivery().instructions(ROLES[CODER], "agent-1")
    assert "### layers\n\nBe layers." in told and "### orbit\n\nBe orbit." in told
    assert told.endswith("`application`, `python`, `layers`, `orbit` apply under `src`, `tests`.")


async def test_a_task_naming_a_mapped_skill_reads_the_other_mapped_ones() -> None:
    read_skill = orbits_delivery().tool(lambda: ROLES[CODER], "agent-1")
    assert await read_skill({"skill": "layers", "file": "SKILL.md"}) == Reply("layers's SKILL.md")


async def test_read_skill_reads_the_skills_the_role_has_at_the_call() -> None:
    roles = [ROLES[CODER].model_copy(update={"skills": ("application",)})]
    read_skill = SkillDelivery(LIBRARY, (), lambda agent_id: None).tool(lambda: roles[-1], None)
    roles.append(ROLES[CODER])
    assert await read_skill({"skill": "application", "file": "SKILL.md"}) == Reply(
        "You weren't given the skill application; your skills are code-style, security.",
        is_error=True,
    )


async def test_reads_are_noted_on_the_agents_process_each_once_and_refusals_are_not() -> None:
    traced = Traced()
    process = AgentProcess(traced.telemetry.start(PROCESS_SPAN))
    read_skill = delivering(process).tool(lambda: ROLES[CODER], "agent-1")
    for name, path in (("python", "testing.md"), ("python", "testing.md"), ("cpp", "SKILL.md")):
        await read_skill({"skill": name, "file": path})
    process.end()
    [span] = traced.spans(PROCESS_SPAN)
    assert attributes(span)["orbit.agent.skill_files"] == ("python/testing.md",)
