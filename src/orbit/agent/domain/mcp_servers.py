"""The external MCP servers a project declares for its roles, ``[mcp.<name>]``: how each is
reached, the user config's overrides of that, merged across the layers, and the checks that the
roles naming them and the tools held for approval agree with the declarations.

Nothing here connects to a server: the declarations reach a workspace's configuration as values.
A value in ``env`` or ``headers`` may reference a variable as ``$NAME`` or ``${NAME}``, which is
filled in when the server starts, so the file never holds a secret's value.
"""

import ipaddress
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from pydantic_core import ErrorDetails, PydanticCustomError

from orbit.agent.domain.config_errors import ConfigError, dotted
from orbit.agent.domain.layers import DEFAULT, Layer, UserConfigError, UserRoles
from orbit.agent.domain.roles import PLAIN_NAME, Role, ServerGrant
from orbit.agent.domain.tools import ORBIT_TOOLS

MCP_TABLE = "mcp"
"""The top-level table the servers are declared in, by name."""

TRANSPORT_FIELDS = frozenset({"command", "url"})
"""A server's fields saying how it's reached, exactly one of which it sets."""

TRANSPORT_RULE = "a server has either a command or a url"
"""Why a server setting both ``command`` and ``url``, or neither, is refused."""

SERVER_FIELD_BELONGS_IN_PROJECT = (
    "belongs in the project's .orbit/config.toml; the user config sets only a server's command,"
    " url, env and headers"
)
"""Why the user config can't set a server's field other than those it may override."""

URL_SCHEMES = frozenset({"http", "https"})
"""The schemes a server's URL may have."""

PLAIN_HTTP_RULE = "url must use https unless it's on this machine."
"""Why a server at a plain ``http`` URL on another machine is refused: its headers, which may
hold credentials, would cross the network in the clear."""

LOOPBACK_NAME = "localhost"
"""The host name that's this machine, besides the loopback addresses."""


def _server_name(name: str) -> str:
    """The server's ``name``, refused unless it's a plain name without ``__``, which parts a
    server's name from its tools' in the names agents see, and isn't one of orbit's tools."""
    if not PLAIN_NAME.fullmatch(name) or "__" in name:
        raise PydanticCustomError(
            "server_name",
            "a server's name is a plain name: letters, digits, - and _, without __",
        )
    if name in ORBIT_TOOLS:
        raise PydanticCustomError(
            "server_name",
            "{name} is one of orbit's tools; give the server another name",
            {"name": name},
        )
    return name


ServerName = Annotated[str, AfterValidator(_server_name)]
"""An MCP server's name, as ``[mcp.<name>]`` gives it."""


def _checked_command(command: tuple[str, ...]) -> tuple[str, ...]:
    """The server's ``command``, refused unless it starts with a program."""
    if not command or not command[0].strip():
        raise ValueError("a command starts with its program")
    return command


Command = Annotated[tuple[str, ...], AfterValidator(_checked_command)]
"""The command that starts a server: its program, then its arguments."""


def _checked_url(url: str) -> str:
    """The server's ``url``, refused unless it's an HTTP or HTTPS URL with a host."""
    parts = urlsplit(url)
    if parts.scheme not in URL_SCHEMES or not parts.netloc:
        raise ValueError(f"{url} isn't an http:// or https:// URL")
    return url


Url = Annotated[str, AfterValidator(_checked_url)]
"""The URL a running server serves MCP at."""


def _is_in_the_clear(url: str) -> bool:
    """Whether ``url`` is plain ``http`` to another machine, so what's sent to it could be read
    on the way."""
    parts = urlsplit(url)
    return parts.scheme == "http" and not _is_loopback(parts.hostname or "")


def _is_loopback(host: str) -> bool:
    """Whether ``host`` is this machine: ``localhost`` or a loopback address."""
    if host.lower() == LOOPBACK_NAME:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _refuse_in_the_clear(url: str | None) -> None:
    """Refuse ``url``, if given, when it's plain ``http`` to another machine."""
    if url is not None and _is_in_the_clear(url):
        raise PydanticCustomError("server_url", PLAIN_HTTP_RULE)


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class McpServer(_Config):
    """An external MCP server roles may use, as the project file declares it: the ``command``
    that starts it or the ``url`` it serves MCP at, exactly one; its ``env`` and, with a URL, its
    HTTP ``headers``, whose values may reference variables; whether agents share one connection
    to it (``shared``); how long an idle connection is kept (``idle_minutes``); how long a call
    may take (``timeout_seconds``); and which of its tools' calls are held for the user's
    approval (``approve``): none, every one, or those it lists."""

    command: Command | None = Field(
        default=None,
        description=(
            "The command that starts it, as a process of orbit's: its program and arguments."
        ),
    )
    url: Url | None = Field(
        default=None, description="The URL it serves MCP at, if orbit doesn't start it."
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Its environment variables; $NAME or ${NAME} in a value is filled in from orbit's."
        ),
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "With a url, its HTTP headers; $NAME or ${NAME} in a value is filled in from orbit's."
        ),
    )
    shared: bool = Field(
        default=True,
        description=(
            "Whether the agents share one connection to it, rather than each having its own."
        ),
    )
    idle_minutes: int = Field(
        default=10, gt=0, description="How long a connection it's idle on is kept, in minutes."
    )
    timeout_seconds: int = Field(
        default=300, gt=0, description="How long a call to one of its tools may take, in seconds."
    )
    approve: bool | tuple[str, ...] = Field(
        default=False,
        description=(
            "Which of its tools' calls wait for your approval: none, all (true), or a list."
        ),
    )

    @model_validator(mode="after")
    def _reached_one_way(self) -> Self:
        if (self.command is None) == (self.url is None):
            raise PydanticCustomError("server_transport", TRANSPORT_RULE)
        if self.headers and self.url is None:
            raise PydanticCustomError("server_headers", "headers go with a url, not a command")
        _refuse_in_the_clear(self.url)
        return self


class ServerFields(_Config):
    """The fields of a server the user config overrides, those bound to the machine: its
    ``command`` or ``url``, ``env`` and ``headers``, each ``None`` if it doesn't set it."""

    command: Command | None = None
    url: Url | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None

    @model_validator(mode="after")
    def _reached_one_way(self) -> Self:
        if self.command is not None and self.url is not None:
            raise PydanticCustomError("server_transport", TRANSPORT_RULE)
        _refuse_in_the_clear(self.url)
        return self


class _UserServers(BaseModel):
    model_config = ConfigDict(frozen=True)

    mcp: dict[str, ServerFields]


@dataclass(frozen=True)
class MergedServers:
    """The servers merged across the project file and the user config but not yet validated:
    each server's ``tables`` of fields, by name, as validating a server takes them, and the
    ``layers`` each field came from, by server and field name."""

    tables: Any
    layers: dict[str, dict[str, Layer]]


def merge_servers(given: object, project: Layer, user: UserRoles) -> MergedServers:
    """The servers the file in ``project`` declares in ``given``, its ``[mcp]`` table, each
    field the ``user`` config's value if it overrides it, else the project's, else the default.
    Setting ``command`` or ``url``, the user config replaces how the server is reached, both
    fields. ``given``, or one of its servers, as it is if it isn't a table, for validation to
    refuse.

    Raises :class:`UserConfigError` naming each of the user config's server fields that's invalid
    or that it can't set, or else each server it overrides that ``given`` doesn't declare.
    """
    overrides = _user_servers(user.servers)
    if not isinstance(given, dict):
        return MergedServers(given, {})
    _refuse_added(overrides, given)
    merged = {
        name: _merged(table, overrides.get(name), project, user.layer)
        for name, table in given.items()
    }
    return _split(merged)


def _refuse_added(overrides: Collection[str], declared: Collection[str]) -> None:
    """Raise :class:`UserConfigError` naming each server the user config ``overrides`` that the
    project hasn't ``declared``, on a line of its own, if any."""
    if added := [name for name in overrides if name not in declared]:
        raise UserConfigError("\n".join(map(added_server_note, added)))


def _split(merged: Mapping[str, tuple[object, dict[str, Layer]]]) -> MergedServers:
    """The ``merged`` servers' tables and layers, each by server, apart."""
    return MergedServers(
        {name: table for name, (table, _) in merged.items()},
        {name: layers for name, (_, layers) in merged.items()},
    )


def added_server_note(name: str) -> str:
    """Why the user config is refused for overriding server ``name``, which the project doesn't
    declare."""
    return f"mcp.{name}: the user config can't add servers; declare it in .orbit/config.toml."


def _user_servers(given: object) -> dict[str, ServerFields]:
    """The server fields the user config overrides in ``given``, its ``[mcp]`` table, by server.

    Raises :class:`UserConfigError` naming each field that's invalid or that it can't set.
    """
    try:
        return _UserServers.model_validate({MCP_TABLE: given}).mcp
    except ValidationError as exc:
        raise UserConfigError(_user_server_problems(exc)) from None


def _user_server_problems(error: ValidationError) -> str:
    """``error``'s problems with the user config's ``[mcp]``, one per line, each naming the key
    and why; a field it can't set belongs in the project file."""
    return "\n".join(f"{dotted(problem['loc'])}: {_why(problem)}" for problem in error.errors())


def _why(problem: ErrorDetails) -> str:
    """Why ``problem``, found in the user config's ``[mcp]``, is one."""
    if problem["type"] == "extra_forbidden":
        return SERVER_FIELD_BELONGS_IN_PROJECT
    return problem["msg"]


def _merged(
    given: object, override: ServerFields | None, project: Layer, user: Layer
) -> tuple[object, dict[str, Layer]]:
    """A server's fields ``given`` in the file in ``project``, with the ``override`` of the user
    config in ``user`` over them, and the layer each came from; ``given`` as it is if it isn't a
    table."""
    if not isinstance(given, dict):
        return given, {}
    users = {} if override is None else override.model_dump(exclude_unset=True)
    replaced = TRANSPORT_FIELDS if users.keys() & TRANSPORT_FIELDS else frozenset()
    fields = {key: value for key, value in given.items() if key not in replaced} | users
    layers = dict.fromkeys(McpServer.model_fields, DEFAULT) | dict.fromkeys(given, project)
    return fields, layers | dict.fromkeys(users.keys() | replaced, user)


def check_role_servers(
    servers: Mapping[str, McpServer], roles: Mapping[str, Role], users: Collection[str] = ()
) -> None:
    """Raise if ``roles`` name MCP servers that ``servers`` doesn't declare, or ``servers`` hold
    calls for approval that no role can make: :class:`ConfigError` naming each problem of the
    project's, on a line of its own, then :class:`UserConfigError` naming each of the roles whose
    servers the user config gives, ``users``.

    A server's ``approve`` list may name only tools some role allowing it lists, when every role
    allowing it lists its tools.
    """
    by_role = {name: _grant_problems(name, role, servers) for name, role in roles.items()}
    project = [*_problems_of(by_role, set(roles) - set(users)), *_approve_problems(servers, roles)]
    if project:
        raise ConfigError("\n".join(project))
    if users_problems := _problems_of(by_role, users):
        raise UserConfigError("\n".join(users_problems))


def _problems_of(by_role: Mapping[str, list[str]], names: Collection[str]) -> list[str]:
    """The problems ``by_role`` gives the roles among ``names``, role by role in its order."""
    return [problem for name, problems in by_role.items() if name in names for problem in problems]


def _grant_problems(name: str, role: Role, servers: Mapping[str, McpServer]) -> list[str]:
    """Why each server role ``name``, ``role``, may use is refused: ``servers`` don't declare
    it."""
    return [
        f"roles.{name}.tools.mcp: {grant.server} isn't a declared MCP server; {_declared(servers)}."
        for grant in role.tools.mcp
        if grant.server not in servers
    ]


def _declared(servers: Collection[str]) -> str:
    """The declared ``servers``, in words."""
    if not servers:
        return "declare it in an [mcp.<name>] table of .orbit/config.toml"
    return f"the declared ones are {', '.join(servers)}"


def _approve_problems(servers: Mapping[str, McpServer], roles: Mapping[str, Role]) -> Iterator[str]:
    """Why each tool a server's ``approve`` list names is refused: some role among ``roles``
    allows the server, every one that does lists its tools, and none lists that one."""
    for name, server in servers.items():
        listed = _listed_tools(_grants_of(name, roles))
        if isinstance(server.approve, bool) or listed is None:
            continue
        yield from (
            f"mcp.{name}.approve: {tool} isn't a tool any role allowing {name} lists; they list"
            f" {', '.join(listed)}."
            for tool in _unlisted(server.approve, listed)
        )


def _unlisted(tools: Iterable[str], listed: Collection[str]) -> list[str]:
    """The ``tools`` that aren't among those ``listed``, in order."""
    return [tool for tool in tools if tool not in listed]


def _grants_of(server: str, roles: Mapping[str, Role]) -> list[ServerGrant]:
    """The ``roles``' grants of ``server``, role by role."""
    return [grant for role in roles.values() for grant in role.tools.mcp if grant.server == server]


def _listed_tools(grants: Sequence[ServerGrant]) -> tuple[str, ...] | None:
    """The tools the ``grants`` of a server list, in order, each once; ``None`` if there are none
    or one allows all its tools."""
    lists = [grant.tools for grant in grants]
    if not lists or None in lists:
        return None
    return _each_once(lists)


def _each_once(lists: Iterable[tuple[str, ...] | None]) -> tuple[str, ...]:
    """The tools ``lists`` name, in order, each once."""
    return tuple(dict.fromkeys(tool for tools in lists if tools for tool in tools))
