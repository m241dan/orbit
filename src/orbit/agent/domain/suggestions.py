"""Tasks agents suggest or propose: the title and prompt their arguments give, the coder's task
they become, and why one is refused.

A suggested task needs a title, kept on one line; its prompt may be empty. It's a coder's task,
so it's refused while the coder is disabled.
"""

from collections.abc import Mapping
from typing import Any

from orbit.agent.domain.roles import Role, disabled_note, disabled_roles, role_task
from orbit.protocol import CODER, TaskSpec


def suggested_task(coder: Role, title: str, prompt: str, skills: tuple[str, ...] = ()) -> TaskSpec:
    """A coder's task an agent suggested or proposed, as ``title`` and ``prompt``, naming the
    ``skills`` its agent gets beyond its role's, none unless given."""
    title, prompt = title.strip(), prompt.strip()
    return role_task(CODER, coder, f"{title}\n\n{prompt}" if prompt else title, skills)


def suggestion_refusal(roles: Mapping[str, Role]) -> str | None:
    """Why a task an agent suggests or proposes is refused, if it is: it's a coder's task, and
    nothing would run it while the coder is disabled among ``roles``."""
    return disabled_note(CODER) if CODER in disabled_roles(roles) else None


def task_suggestion(arguments: Mapping[str, Any]) -> tuple[str, str] | str:
    """The title, on one line, and prompt of the task an agent suggested or proposed with
    ``arguments``; or why it's refused."""
    title = " ".join(str(arguments.get("title", "")).split())
    if not title:
        return "A task needs a title."
    return title, str(arguments.get("prompt", "")).strip()
