"""The agents orbit has started, and the tokens that tell their requests apart."""

import itertools
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Agent:
    """An agent started in ``role`` to work on task ``task_id``.

    It sends ``token`` with every request, so orbit knows who's asking without trusting
    anything the model says about itself.
    """

    id: str
    role: str
    task_id: str
    token: str


class AgentRegistry:
    """Hands out agent ids and one-time tokens, and resolves tokens back to agents.

    ``new_token`` makes the tokens; they must be unguessable. Ids aren't reused, and a
    revoked token stops resolving, so a lingering process can't act for a later agent.
    """

    def __init__(self, new_token: Callable[[], str]) -> None:
        self._new_token = new_token
        self._ids = itertools.count(1)
        self._by_token: dict[str, Agent] = {}
        self._used: set[str] = set()

    @property
    def live(self) -> tuple[Agent, ...]:
        """Agents whose tokens still resolve, oldest first."""
        return tuple(self._by_token.values())

    def register(self, role: str, task_id: str) -> Agent:
        token = self._new_token()
        if token in self._used:
            raise RuntimeError("The token factory repeated a token.")
        self._used.add(token)
        agent = Agent(id=f"agent-{next(self._ids)}", role=role, task_id=task_id, token=token)
        self._by_token[token] = agent
        return agent

    def by_token(self, token: str) -> Agent | None:
        return self._by_token.get(token)

    def revoke(self, agent_id: str) -> None:
        """Stop agent ``agent_id``'s token from resolving; harmless if it already doesn't."""
        self._by_token = {t: a for t, a in self._by_token.items() if a.id != agent_id}
