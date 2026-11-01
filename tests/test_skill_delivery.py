"""Skills delivered in agents' instructions: an agent, a task's or a planner turn's, on any
runtime, is launched with its role's instructions and then the skills it was given, its role's and
the project's mapped ones, and its process's span records those skills and the files of them it
read."""

import asyncio
from collections.abc import Mapping
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan

from orbit.agent.application.agent_trace import PROCESS_SPAN
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.ports import AgentLaunch, Reply
from orbit.agent.domain.layers import PACKAGE
from orbit.agent.domain.roles import READ_SKILL
from orbit.agent.domain.skills import Skill, SkillFile, SkillLibrary, SkillMapping
from orbit.agent.external import claude_cli, kiro_cli
from orbit.agent.external.kiro_agents import kiro_agent_config
from orbit.protocol import (
    CODER,
    PLANNER,
    ActivateSession,
    ConversationReset,
    Event,
    ResetConversation,
    TurnEndReason,
)
from tests.agent_fakes import (
    ROLES,
    Call,
    Script,
    agents_for,
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


LIBRARY = SkillLibrary(skills={name: skill(name) for name in ("code-style", "python", "security")})
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


async def coder_launch(*scripts: Script, **options: Any) -> AgentLaunch:
    """How a coder playing ``scripts``, or :func:`claims_done`, is launched in a session given
    ``options``, such as ``skill_library``."""
    session, _, launcher = session_for(*(scripts or (claims_done,)), **options)
    await asyncio.wait_for(run(session), 5)
    await session.close()
    [launch] = launcher.launches
    return launch


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
        "`python`, `code-style` apply under `src`, `tests`."
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
        "`security` applies under `src`."
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
    assert before.endswith("### code-style\n\nBe code-style.")
    assert (
        after
        == f"You are orbit's planner.\n\n## Skills\n\n{READING}\n\n### security\n\nBe security."
    )


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
