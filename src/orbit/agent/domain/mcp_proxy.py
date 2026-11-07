"""The rules of proxying external MCP servers' tools to agents: the names agents see them by,
``<server>__<tool>``, made safe and unique among the server's, which of a server's tools a role
allows, the variables a server's environment and headers are filled from, which calls wait for
the user's approval, and the wording of refusals and notices."""

import hashlib
import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orbit.agent.domain.mcp_servers import McpServer
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


@dataclass(frozen=True)
class FilledServer:
    """A server's ``env`` and ``headers``, their variables filled in."""

    env: dict[str, str]
    headers: dict[str, str]


@dataclass(frozen=True)
class RefusedCredentials:
    """Why a server failed: it refused orbit's credentials, answering with HTTP ``status``."""

    status: int


type FailureReason = str | RefusedCredentials
"""Why a server failed: in words, a sentence or not, or its refusal of orbit's credentials."""


def name_proxied_tool(server: str, tool: str) -> str:
    """The name agents see ``server``'s tool ``tool`` by: ``<server>__<tool>``, each character an
    MCP tool name can't have replaced by ``_``, and cut to :data:`NAME_LIMIT` with a short hash
    of the whole name at its end if it's longer."""
    whole = f"{server}{SEPARATOR}{tool}"
    name = _UNSAFE.sub("_", whole)
    return name if len(name) <= NAME_LIMIT else _hashed(name, whole)


def _hashed(name: str, whole: str) -> str:
    """``name`` ending in a short hash of ``whole``, the name it stands for, cut so the two fit
    in :data:`NAME_LIMIT`."""
    digest = hashlib.sha256(whole.encode()).hexdigest()[:HASH_LENGTH]
    return f"{name[: NAME_LIMIT - HASH_LENGTH - 1]}-{digest}"


def name_unique_tools(server: str, offered: Sequence[UpstreamTool]) -> tuple[str, ...]:
    """The names agents see the tools ``server`` ``offered`` by, in its order, each naming one
    tool: a tool's :func:`name_proxied_tool`, unless it had characters replaced and another tool has
    it too, when it ends in a short hash of ``<server>__<tool>`` instead."""
    wholes = [f"{server}{SEPARATOR}{tool.name}" for tool in offered]
    names = [name_proxied_tool(server, tool.name) for tool in offered]
    counts = Counter(names)
    return tuple(
        _hashed(name, whole) if counts[name] > 1 and name != whole else name
        for name, whole in zip(names, wholes, strict=True)
    )


def is_proxied(name: str) -> bool:
    """Whether ``name`` is one agents see a server's tool by, rather than one of orbit's."""
    return SEPARATOR in name


def find_grant(role: Role, server: str) -> ServerGrant | None:
    """``role``'s grant of ``server``, if it allows it."""
    return next((grant for grant in role.tools.mcp if grant.server == server), None)


def list_proxied_tools(
    server: str, grant: ServerGrant, offered: Sequence[UpstreamTool]
) -> tuple[ProxiedTool, ...]:
    """The tools ``server`` ``offered`` that ``grant`` allows, in its order, as agents see them,
    by their :func:`name_unique_tools`: all of them, if it lists none."""
    return tuple(
        ProxiedTool(name, server, tool)
        for name, tool in zip(name_unique_tools(server, offered), offered, strict=True)
        if grant.tools is None or tool.name in grant.tools
    )


def find_proxied(tools: Sequence[ProxiedTool], name: str) -> ProxiedTool | None:
    """The tool among ``tools`` that agents see as ``name``, if there's one."""
    return next((tool for tool in tools if tool.name == name), None)


def word_missing_tool_notices(
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
        grant = find_grant(role, server)
        listed = () if grant is None or grant.tools is None else grant.tools
        yield from ((name, tool) for tool in listed)


def find_variables(env: Mapping[str, str]) -> tuple[str, ...]:
    """The variables ``env``'s values reference as ``$NAME`` or ``${NAME}``, each once, in
    order."""
    found = (
        match["braced"] or match["bare"]
        for value in env.values()
        for match in _VARIABLE.finditer(value)
    )
    return tuple(dict.fromkeys(found))


def fill_env(env: Mapping[str, str], values: Mapping[str, str]) -> dict[str, str] | UnsetVariable:
    """``env`` with each variable its values reference filled in from ``values``, or the first
    variable ``values`` lacks."""
    unset = [name for name in find_variables(env) if name not in values]
    if unset:
        return UnsetVariable(unset[0])
    return {
        key: _VARIABLE.sub(lambda match: values[match["braced"] or match["bare"]], value)
        for key, value in env.items()
    }


def find_server_variables(server: McpServer) -> tuple[str, ...]:
    """The variables ``server``'s ``env``, then its ``headers``, reference, each once, in
    order."""
    return tuple(dict.fromkeys((*find_variables(server.env), *find_variables(server.headers))))


def fill_server(server: McpServer, values: Mapping[str, str]) -> FilledServer | UnsetVariable:
    """``server``'s ``env`` and ``headers`` with each variable they reference filled in from
    ``values``, or the first variable ``values`` lacks."""
    env = fill_env(server.env, values)
    if isinstance(env, UnsetVariable):
        return env
    headers = fill_env(server.headers, values)
    if isinstance(headers, UnsetVariable):
        return headers
    return FilledServer(env, headers)


def needs_approval(server: McpServer, tool: str) -> bool:
    """Whether a call to ``server``'s tool ``tool``, by its name on the server, waits for the
    user's approval: the server's ``approve`` is ``true``, or lists the tool."""
    return server.approve is True or (
        not isinstance(server.approve, bool) and tool in server.approve
    )


def word_declined(name: str) -> str:
    """The answer to a call to the tool ``name`` that the user declined, or that expired."""
    return f"The user declined {name}."


def word_refusal(role: str, name: str) -> str:
    """Why a call from an agent in role ``role`` to the tool ``name`` is refused."""
    return f"roles.{role} doesn't allow {name}."


def word_call_failure(server: str, reason: FailureReason) -> str:
    """The answer to a call to a tool of ``server``, which failed for ``reason``."""
    if isinstance(reason, RefusedCredentials):
        return f"MCP server {server} refused orbit's credentials (HTTP {reason.status})."
    return f"MCP server {server} failed: {reason.rstrip('.')}."


def word_failure_notice(server: str, reason: FailureReason) -> str:
    """The notice that ``server`` failed, for ``reason``, starting a streak of failures."""
    return f"{word_call_failure(server, reason)} Agents' calls to it return errors until it starts."
