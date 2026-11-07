"""The rule of which skills an agent is given, which a server and its frontends both follow:
its role's, then its task's, then those the project's ``[[skills]]`` entries map, each once."""

from collections.abc import Sequence


def given_skills(
    role_skills: Sequence[str], task_skills: Sequence[str], mapped_skills: Sequence[str]
) -> tuple[str, ...]:
    """The skills an agent is given, by name: its role's ``role_skills``, then its task's
    ``task_skills``, then the ``mapped_skills`` the project's ``[[skills]]`` entries use, each
    once, where it's first named."""
    return tuple(dict.fromkeys((*role_skills, *task_skills, *mapped_skills)))
