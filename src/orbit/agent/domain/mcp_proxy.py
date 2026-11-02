"""The rules of proxying external MCP servers' tools to agents: the names agents see them by,
``<server>__<tool>``, which of a server's tools a role allows, the variables a server's
environment is filled from, and the wording of refusals and notices."""

import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orbit.agent.domain.roles import Role, ServerGrant

SEPARATOR = "__"
"""What parts a server's name from its tool's in the names agents see; servers can't have it."""

NAME_LIMIT = 128
"""The longest name an MCP tool may have."""

HASH_LENGTH = 8
"""How many hex digits of its hash end a name cut to :data:`NAME_LIMIT`."""

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_VARIABLE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)


@dataclass(frozen=True)
class UpstreamTool:
    """A tool an MCP server offers: its ``name`` on the server, its ``description``, if it has
    one, and the JSON schema of its arguments, ``input_schema``, as the server gives them."""

    name: str
    description: str | None
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ProxiedTool:
    """A server's tool as agents see it: by orbit's ``name`` for it, the ``server``'s ``tool``."""

    name: str
    server: str
    tool: UpstreamTool


@dataclass(frozen=True)
class UnsetVariable:
    """A variable, ``name``, that a server's environment references and nothing sets."""

    name: str

    @property
    def message(self) -> str:
        """Why the server can't start, in words."""
        return f"{self.name} isn't set in orbit's environment or credentials.toml."


def proxied_name(server: str, tool: str) -> str:
    """The name agents see ``server``'s tool ``tool`` by: ``<server>__<tool>``, each character an
    MCP tool name can't have replaced by ``_``, and cut to :data:`NAME_LIMIT` with a short hash
    of the whole name at its end if it's longer."""
    name = _UNSAFE.sub("_", f"{server}{SEPARATOR}{tool}")
    if len(name) <= NAME_LIMIT:
        return name
    digest = hashlib.sha256(f"{server}{SEPARATOR}{tool}".encode()).hexdigest()[:HASH_LENGTH]
    return f"{name[: NAME_LIMIT - HASH_LENGTH - 1]}-{digest}"


def is_proxied(name: str) -> bool:
    """Whether ``name`` is one agents see a server's tool by, rather than one of orbit's."""
    return SEPARATOR in name


def grant_for(role: Role, server: str) -> ServerGrant | None:
    """``role``'s grant of ``server``, if it allows it."""
    return next((grant for grant in role.tools.mcp if grant.server == server), None)


def proxied_tools(
    server: str, grant: ServerGrant, offered: Sequence[UpstreamTool]
) -> tuple[ProxiedTool, ...]:
    """The tools ``server`` ``offered`` that ``grant`` allows, in its order, as agents see them:
    all of them, if it lists none."""
    return tuple(
        ProxiedTool(proxied_name(server, tool.name), server, tool)
        for tool in offered
        if grant.tools is None or tool.name in grant.tools
    )


def find_proxied(tools: Sequence[ProxiedTool], name: str) -> ProxiedTool | None:
    """The tool among ``tools`` that agents see as ``name``, if there's one."""
    return next((tool for tool in tools if tool.name == name), None)


def missing_tool_notices(
    server: str, roles: Mapping[str, Role], offered: Sequence[UpstreamTool]
) -> tuple[str, ...]:
    """A notice for each tool one of ``roles`` lists of ``server`` that the server doesn't
    offer, as ``offered`` says, role by role."""
    names = {tool.name for tool in offered}
    return tuple(
        f"{server} offers no tool {tool}, which roles.{role}.tools.mcp names."
        for role, tool in _listed(server, roles)
        if tool not in names
    )


def _listed(server: str, roles: Mapping[str, Role]) -> Iterator[tuple[str, str]]:
    """Each role among ``roles`` that lists tools of ``server``, by name, with each it lists."""
    for name, role in roles.items():
        grant = grant_for(role, server)
        listed = () if grant is None or grant.tools is None else grant.tools
        yield from ((name, tool) for tool in listed)


def variables_in(env: Mapping[str, str]) -> tuple[str, ...]:
    """The variables ``env``'s values reference as ``$NAME`` or ``${NAME}``, each once, in
    order."""
    found = (
        match["braced"] or match["bare"]
        for value in env.values()
        for match in _VARIABLE.finditer(value)
    )
    return tuple(dict.fromkeys(found))


def filled_env(env: Mapping[str, str], values: Mapping[str, str]) -> dict[str, str] | UnsetVariable:
    """``env`` with each variable its values reference filled in from ``values``, or the first
    variable ``values`` lacks."""
    unset = [name for name in variables_in(env) if name not in values]
    if unset:
        return UnsetVariable(unset[0])
    return {
        key: _VARIABLE.sub(lambda match: values[match["braced"] or match["bare"]], value)
        for key, value in env.items()
    }


def refusal(role: str, name: str) -> str:
    """Why a call from an agent in role ``role`` to the tool ``name`` is refused."""
    return f"roles.{role} doesn't allow {name}."


def call_failure(server: str, reason: str) -> str:
    """The answer to a call to a tool of ``server``, which failed for ``reason``, a sentence
    or not."""
    return f"MCP server {server} failed: {reason.rstrip('.')}."


def failure_notice(server: str, reason: str) -> str:
    """The notice that ``server`` failed, for ``reason``, a sentence or not, starting a streak
    of failures."""
    return f"{call_failure(server, reason)} Agents' calls to it return errors until it starts."
