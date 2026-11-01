"""``read_skill``: what an agent, a task's or a conversation's, reads of the skills it was given,
its role's and the project's mapped ones, and what it's refused."""

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan

from orbit.agent.application.agent_trace import REQUEST_SPAN
from orbit.agent.application.ports import Reply
from orbit.agent.domain.layers import PACKAGE
from orbit.agent.domain.roles import Role
from orbit.agent.domain.skills import Skill, SkillFile, SkillLibrary, SkillMapping
from orbit.protocol import CODER, PLANNER, Event, SendMessage, TurnEndReason, TurnFinished
from tests.agent_fakes import ROLES, Call, Script, events_until, run, send, session_for
from tests.telemetry_helpers import Traced


def skill(name: str, *paths: str) -> Skill:
    """The skill ``name``, with its SKILL.md and a file at each of ``paths``, each holding its
    skill's name and its path."""
    files = {
        path: SkillFile(text=f"{name}'s {path}", layer=PACKAGE, location=f"/s/{name}/{path}")
        for path in sorted(("SKILL.md", *paths))
    }
    return Skill(name=name, description=f"{name}.", body="", files=files)


LIBRARY = SkillLibrary(
    skills={
        "code-style": skill("code-style", "testing.md", "naming.md"),
        "python": skill("python", "tests.md"),
        "security": skill("security"),
    }
)
"""The skills a session's agents can be given."""

PYTHON = (SkillMapping(paths=("src",), use=("python",)),)
"""A project mapping its ``src`` to ``python``."""

STYLE_FILES = "SKILL.md, naming.md, testing.md"
"""code-style's files, sorted."""


def reads(heard: list[Reply], *made: tuple[str, str]) -> Script:
    """A coder's turn reading each skill's file ``made`` names, noting what it hears in
    ``heard``."""

    async def turn(call: Call) -> TurnEndReason:
        await call("get_task")
        for name, path in made:
            heard.append(await call("read_skill", skill=name, file=path))
        await call("complete_task", summary="Read.")
        return "completed"

    return turn


async def coder_hears(*made: tuple[str, str], **options: Any) -> list[Reply]:
    """What a coder hears reading the skills' files ``made`` names, in a session given
    :data:`LIBRARY` and ``options``."""
    heard: list[Reply] = []
    session, _, _ = session_for(reads(heard, *made), skill_library=LIBRARY, **options)
    await asyncio.wait_for(run(session), 5)
    await session.close()
    return heard


def finished(agent_id: str) -> Callable[[Event], bool]:
    """Stops at agent ``agent_id``'s first TurnFinished."""
    return lambda event: isinstance(event, TurnFinished) and event.agent_id == agent_id


async def test_a_coder_reads_a_file_of_its_roles_skill() -> None:
    assert await coder_hears(("code-style", "testing.md")) == [Reply("code-style's testing.md")]


async def test_a_coder_reads_its_skills_skill_md() -> None:
    assert await coder_hears(("security", "SKILL.md")) == [Reply("security's SKILL.md")]


async def test_a_skill_neither_its_role_nor_a_mapping_gives_is_refused_listing_its_skills() -> None:
    assert await coder_hears(("python", "tests.md")) == [
        Reply(
            "You weren't given the skill python; your skills are code-style, security.",
            is_error=True,
        )
    ]


async def test_a_mapped_skill_is_given_along_with_the_roles() -> None:
    heard = await coder_hears(("python", "tests.md"), ("pyton", "tests.md"), skill_mappings=PYTHON)
    assert heard == [
        Reply("python's tests.md"),
        Reply(
            "You weren't given the skill pyton; your skills are code-style, security, python.",
            is_error=True,
        ),
    ]


@pytest.mark.parametrize("path", ["nope.md", "../python/tests.md", "/etc/passwd"])
async def test_a_file_the_skill_lacks_is_refused_listing_its_files(path: str) -> None:
    assert await coder_hears(("code-style", path)) == [
        Reply(f"code-style has no file {path}; its files are {STYLE_FILES}.", is_error=True)
    ]


async def test_a_role_with_no_skills_and_no_mapping_is_told_it_was_given_none() -> None:
    roles = {**ROLES, CODER: ROLES[CODER].model_copy(update={"skills": ()})}
    assert await coder_hears(("code-style", "testing.md"), roles=roles) == [
        Reply("You weren't given any skills.", is_error=True)
    ]


async def test_a_call_without_arguments_asks_for_an_empty_skill() -> None:
    heard: list[Reply] = []

    async def bare(call: Call) -> TurnEndReason:
        await call("get_task")
        heard.append(await call("read_skill"))
        await call("complete_task", summary="Read.")
        return "completed"

    session, _, _ = session_for(bare, skill_library=LIBRARY)
    await asyncio.wait_for(run(session), 5)
    await session.close()
    assert heard == [
        Reply("You weren't given the skill ; your skills are code-style, security.", is_error=True)
    ]


async def planner_hears(planner: Role, *made: tuple[str, str]) -> list[Reply]:
    """What the planner, in the role ``planner``, hears reading the skills' files ``made``
    names in a turn, in a session given :data:`LIBRARY` and :data:`PYTHON`."""
    heard: list[Reply] = []

    async def turn(call: Call) -> TurnEndReason:
        for name, path in made:
            heard.append(await call("read_skill", skill=name, file=path))
        return "completed"

    roles = {**ROLES, PLANNER: planner}
    session, _, launcher = session_for(roles=roles, skill_library=LIBRARY, skill_mappings=PYTHON)
    launcher.planner_turns = [turn]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, finished("agent-1"))
    await session.close()
    return heard


async def test_the_planner_reads_its_roles_and_the_mapped_skills() -> None:
    heard = await planner_hears(
        ROLES[PLANNER],
        ("code-style", "naming.md"),
        ("python", "SKILL.md"),
        ("security", "SKILL.md"),
    )
    assert heard == [
        Reply("code-style's naming.md"),
        Reply("python's SKILL.md"),
        Reply(
            "You weren't given the skill security; your skills are code-style, python.",
            is_error=True,
        ),
    ]


async def test_the_planner_reads_the_skills_its_role_has_at_the_call() -> None:
    planner = ROLES[PLANNER].model_copy(update={"skills": ("security",)})
    assert await planner_hears(planner, ("security", "SKILL.md")) == [Reply("security's SKILL.md")]


def attributes(span: ReadableSpan) -> Mapping[str, Any]:
    assert span.attributes is not None
    return span.attributes


async def test_each_read_skill_request_records_the_skill_and_file_asked_even_when_refused() -> None:
    traced = Traced()
    heard: list[Reply] = []
    script = reads(heard, ("code-style", "testing.md"), ("python", "../x.md"))
    session, _, _ = session_for(script, skill_library=LIBRARY, telemetry=traced.telemetry)
    await asyncio.wait_for(run(session), 5)
    await session.close()

    spans = sorted(traced.spans(REQUEST_SPAN), key=lambda span: span.start_time or 0)
    reading = [dict(attributes(span)) for span in spans[1:3]]
    assert reading == [
        {
            "orbit.agent.tool": "read_skill",
            "orbit.skill.name": "code-style",
            "orbit.skill.file": "testing.md",
            "orbit.agent.request.error": False,
        },
        {
            "orbit.agent.tool": "read_skill",
            "orbit.skill.name": "python",
            "orbit.skill.file": "../x.md",
            "orbit.agent.request.error": True,
        },
    ]
    assert "orbit.skill.name" not in attributes(spans[0])
