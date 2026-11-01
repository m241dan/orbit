"""Skill delivery: the skills each agent is given, its role's and its task's, delivered in its
instructions, read through ``read_skill`` and noted on its process as it reads them (see
``docs/00037-feature-skill-delivery.md``)."""

from collections.abc import Callable, Sequence
from typing import Any

from orbit.agent.application.agent_trace import AgentProcess, skill_attributes
from orbit.agent.application.ports import Reply, Tool
from orbit.agent.domain.roles import Role
from orbit.agent.domain.skills import (
    SkillLibrary,
    SkillMapping,
    delivered_skills,
    given_skill_file,
    given_skills,
    skill_catalogue,
    skill_instructions,
)
from orbit.tracing import current_span


class SkillDelivery:
    """Delivers agents the skills they're given from the ``library``: their role's, then their
    task's, or, for a task naming none, those the project's ``mappings`` use. The files an agent
    reads are noted on its process, the one ``process_of`` finds by the agent's id while it's
    traced."""

    def __init__(
        self,
        library: SkillLibrary,
        mappings: Sequence[SkillMapping],
        process_of: Callable[[str], AgentProcess | None],
    ) -> None:
        self._library = library
        self._mappings = tuple(mappings)
        self._process_of = process_of
        # The skills each opened agent's task gives it beyond its role's, by its id.
        self._task_skills: dict[str, tuple[str, ...]] = {}

    def open(self, agent_id: str, skills: Sequence[str]) -> None:
        """Give agent ``agent_id`` the ``skills`` its task names beyond its role's, none for a
        conversational agent."""
        self._task_skills[agent_id] = tuple(skills)

    def close(self, agent_id: str) -> None:
        """Forget the skills agent ``agent_id``'s task gave it."""
        self._task_skills.pop(agent_id, None)

    def instructions(self, role: Role, agent_id: str | None) -> str:
        """What agent ``agent_id`` in ``role`` is told: its role's instructions, then, each after
        a blank line, the skills it's given, if any, and, if it proposes tasks, the skills it can
        give them."""
        skills = skill_instructions(
            role.skills, self._skills_of_task(agent_id), self._mappings, self._library
        )
        catalogue = skill_catalogue(self._library) if role.proposes_tasks else ""
        return "\n\n".join((role.instructions, *(part for part in (skills, catalogue) if part)))

    def delivered(self, role: Role, agent_id: str | None) -> tuple[str, ...]:
        """The skills delivered to agent ``agent_id`` in ``role``, by name, in order."""
        return delivered_skills(
            role.skills, self._skills_of_task(agent_id), self._mappings, self._library
        )

    def tool(self, role: Callable[[], Role], agent_id: str | None) -> Tool:
        """``read_skill`` as agent ``agent_id``, once it's opened, calls it, in the role ``role``
        gives at each call, which a reset may have changed."""

        async def read_skill(arguments: dict[str, Any]) -> Reply:
            return self._read_skill(role().skills, agent_id, arguments)

        return read_skill

    def _read_skill(
        self, role_skills: Sequence[str], agent_id: str | None, arguments: dict[str, Any]
    ) -> Reply:
        """The file of one of its skills that agent ``agent_id``, whose role has ``role_skills``,
        asks for in ``arguments``, by the skill's name and the file's path, or why it's refused,
        recording what was asked on the call's span, the current one, and a file it read on its
        process's. Its task's skills are those it was opened with."""
        skill, path = str(arguments.get("skill", "")), str(arguments.get("file", ""))
        current_span().set_attributes(skill_attributes(skill, path))
        given = given_skills(role_skills, self._skills_of_task(agent_id), self._mappings)
        found = given_skill_file(given, self._library, skill, path)
        if isinstance(found, str):
            return Reply(found, is_error=True)
        self._note_read(agent_id, skill, path)
        return Reply(found.text)

    def _note_read(self, agent_id: str | None, skill: str, path: str) -> None:
        """Tally agent ``agent_id``'s reading the file at ``path`` of the skill ``skill`` on its
        process, if it's opened and its process is traced."""
        process = None if agent_id is None else self._process_of(agent_id)
        if process is not None:
            process.read_skill(skill, path)

    def _skills_of_task(self, agent_id: str | None) -> tuple[str, ...]:
        """The skills agent ``agent_id``'s task gives it beyond its role's; none for an agent
        that isn't opened, a conversational one, which has no task, or one whose task names
        none."""
        return () if agent_id is None else self._task_skills.get(agent_id, ())
