"""The external MCP servers a project declares: their model, the user config's overrides merged
over them, and the checks against the roles that name them."""

from typing import Any

import pytest
from pydantic import ValidationError

from orbit.agent.domain.config import load_config
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import DEFAULT, Layer, UserConfigError, UserRoles
from orbit.agent.domain.mcp_servers import (
    McpServer,
    MergedServers,
    check_role_servers,
    merge_servers,
)
from orbit.agent.domain.roles import Role, RoleTools
from orbit.agent.domain.tools import ORBIT_TOOLS

USER = Layer("user", "/home/me/.config/orbit/config.toml")
PROJECT = Layer("project", "/src/app/.orbit/config.toml")
GITHUB = {"command": ["github-mcp-server", "stdio"], "env": {"GITHUB_TOKEN": "$GITHUB_TOKEN"}}
SEMGREP = {"url": "http://127.0.0.1:3010/mcp"}
SERVERS = {
    "github": McpServer.model_validate(GITHUB),
    "semgrep": McpServer.model_validate(SEMGREP),
}


def merged(given: object, user: object = None) -> MergedServers:
    """The servers ``given`` in the project file merge to under a user config overriding
    ``user``, its ``[mcp]`` table, if given."""
    return merge_servers(given, PROJECT, UserRoles(USER, servers=user or {}))


def role(*mcp: object) -> Role:
    """A task role whose agents may use the MCP servers ``mcp``, as a file gives them."""
    return Role(instructions="Work.", tools=RoleTools.model_validate({"mcp": mcp}))


def problems(error: ValidationError) -> list[str]:
    """``error``'s messages, in order."""
    return [problem["msg"] for problem in error.errors()]


# The model


def test_a_server_started_by_a_command_has_the_defaults() -> None:
    server = McpServer.model_validate(GITHUB)
    assert server.command == ("github-mcp-server", "stdio")
    assert server.env == {"GITHUB_TOKEN": "$GITHUB_TOKEN"}
    assert (server.url, server.headers) == (None, {})
    assert (server.shared, server.idle_minutes, server.timeout_seconds) == (True, 10, 300)
    assert server.approve is False


def test_a_server_at_a_url_takes_headers() -> None:
    server = McpServer(url="https://mcp.example.com/mcp", headers={"Authorization": "${TOKEN}"})
    assert server.headers == {"Authorization": "${TOKEN}"}


@pytest.mark.parametrize("approve", [True, ("create_issue",), ()])
def test_approve_is_a_bool_or_a_list_of_tools(approve: bool | tuple[str, ...]) -> None:
    assert McpServer(url="http://h/mcp", approve=approve).approve == approve


@pytest.mark.parametrize(
    "fields",
    [{**GITHUB, **SEMGREP}, {"env": {"A": "b"}}],
    ids=["both", "neither"],
)
def test_a_server_needs_exactly_one_of_a_command_and_a_url(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as raised:
        McpServer.model_validate(fields)
    assert problems(raised.value) == ["a server has either a command or a url"]


def test_headers_with_a_command_are_refused() -> None:
    with pytest.raises(ValidationError) as raised:
        McpServer.model_validate({**GITHUB, "headers": {"Authorization": "x"}})
    assert problems(raised.value) == ["headers go with a url, not a command"]


def test_no_headers_with_a_command_are_no_headers() -> None:
    assert McpServer.model_validate({**GITHUB, "headers": {}}).headers == {}


@pytest.mark.parametrize("command", [[], [" ", "stdio"]])
def test_a_command_without_a_program_is_refused(command: list[str]) -> None:
    with pytest.raises(ValidationError, match="a command starts with its program"):
        McpServer.model_validate({"command": command})


@pytest.mark.parametrize("url", ["ftp://host/mcp", "127.0.0.1:3010/mcp", "http:///mcp"])
def test_a_url_that_isnt_http_with_a_host_is_refused(url: str) -> None:
    with pytest.raises(ValidationError, match="isn't an http:// or https:// URL"):
        McpServer(url=url)


@pytest.mark.parametrize("field", ["idle_minutes", "timeout_seconds"])
def test_waits_are_more_than_zero(field: str) -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        McpServer.model_validate({**SEMGREP, field: 0})


def test_a_server_refuses_fields_it_doesnt_have() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        McpServer.model_validate({**SEMGREP, "transport": "sse"})


# Merging the project's servers with the user config's overrides


def test_without_overrides_each_field_is_the_projects_or_the_default() -> None:
    servers = merged({"github": GITHUB})
    assert servers.tables == {"github": GITHUB}
    layers = servers.layers["github"]
    assert (layers["command"], layers["env"]) == (PROJECT, PROJECT)
    assert (layers["url"], layers["shared"], layers["approve"]) == (DEFAULT, DEFAULT, DEFAULT)


def test_the_users_command_replaces_the_projects() -> None:
    servers = merged({"github": GITHUB}, {"github": {"command": ["/opt/bin/github-mcp", "stdio"]}})
    assert servers.tables["github"]["command"] == ("/opt/bin/github-mcp", "stdio")
    assert servers.tables["github"]["env"] == GITHUB["env"]
    assert servers.layers["github"]["command"] == USER
    assert servers.layers["github"]["env"] == PROJECT


def test_the_users_url_replaces_how_the_server_is_reached() -> None:
    servers = merged({"github": GITHUB}, {"github": {"url": "http://127.0.0.1:9/mcp"}})
    assert servers.tables["github"] == {"env": GITHUB["env"], "url": "http://127.0.0.1:9/mcp"}
    assert servers.layers["github"]["command"] == servers.layers["github"]["url"] == USER


def test_the_users_env_and_headers_replace_the_projects_whole() -> None:
    given = {"semgrep": {**SEMGREP, "headers": {"A": "1", "B": "2"}, "env": {"X": "1"}}}
    servers = merged(given, {"semgrep": {"headers": {"A": "$A"}, "env": {}}})
    assert servers.tables["semgrep"] == {**SEMGREP, "headers": {"A": "$A"}, "env": {}}
    assert servers.layers["semgrep"]["url"] == PROJECT


def test_a_server_the_user_config_leaves_alone_is_the_projects() -> None:
    servers = merged({"github": GITHUB, "semgrep": SEMGREP}, {"github": {"env": {}}})
    assert servers.tables["semgrep"] == SEMGREP


def test_overriding_a_server_the_project_doesnt_declare_is_the_user_configs_fault() -> None:
    with pytest.raises(UserConfigError) as raised:
        merged({"github": GITHUB}, {"other": {"command": ["other-mcp"]}})
    assert str(raised.value) == (
        "mcp.other: the user config can't add servers; declare it in .orbit/config.toml."
    )


def test_a_field_the_user_config_cant_override_belongs_in_the_project() -> None:
    with pytest.raises(UserConfigError) as raised:
        merged({"github": GITHUB}, {"github": {"shared": False, "approve": True}})
    belongs = (
        "belongs in the project's .orbit/config.toml; the user config sets only a server's"
        " command, url, env and headers"
    )
    assert str(raised.value).splitlines() == [
        f"mcp.github.shared: {belongs}",
        f"mcp.github.approve: {belongs}",
    ]


@pytest.mark.parametrize(
    ("user", "message"),
    [
        ({"github": {**GITHUB, **SEMGREP}}, "mcp.github: a server has either a command or a url"),
        ({"github": {"command": "github-mcp"}}, "mcp.github.command: Input should be a valid"),
        ({"github": {"url": "ftp://h"}}, "mcp.github.url: Value error, ftp://h isn't"),
        ({"github": "github-mcp"}, "mcp.github: Input should be a valid dictionary"),
        (["github"], "mcp: Input should be a valid dictionary"),
    ],
)
def test_an_invalid_override_is_the_user_configs_fault(user: object, message: str) -> None:
    with pytest.raises(UserConfigError) as raised:
        merged({"github": GITHUB}, user)
    assert str(raised.value).startswith(message)


def test_a_projects_servers_that_arent_a_table_are_left_for_validation() -> None:
    assert merged(["github"]) == MergedServers(["github"], {})


def test_a_projects_server_that_isnt_a_table_is_left_for_validation() -> None:
    servers = merged({"github": "github-mcp-server"}, {"github": {"env": {}}})
    assert servers.tables == {"github": "github-mcp-server"}
    assert servers.layers == {"github": {}}


# A server's name


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("a__b", "a server's name is a plain name: letters, digits, - and _, without __"),
        ("git.hub", "a server's name is a plain name: letters, digits, - and _, without __"),
        ("get_task", "get_task is one of orbit's tools; give the server another name"),
    ],
)
def test_a_server_name_is_plain_without_a_double_underscore_or_an_orbit_tools(
    name: str, message: str
) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config({"mcp": {name: SEMGREP}}, {})
    assert str(raised.value) == f"mcp.{name}: {message}"


@pytest.mark.parametrize("tool", ORBIT_TOOLS)
def test_no_orbit_tool_names_a_server(tool: str) -> None:
    with pytest.raises(ConfigError, match="is one of orbit's tools"):
        load_config({"mcp": {tool: SEMGREP}}, {})


# The roles that name servers


def test_roles_naming_declared_servers_pass() -> None:
    roles = {"coder": role("semgrep", {"server": "github", "tools": ["get_issue"]})}
    check_role_servers(SERVERS, roles)


def test_a_role_naming_an_undeclared_server_is_refused_naming_the_role() -> None:
    with pytest.raises(ConfigError) as raised:
        check_role_servers(SERVERS, {"coder": role("nope"), "reviewer": role("semgrep")})
    assert str(raised.value) == (
        "roles.coder.tools.mcp: nope isn't a declared MCP server; the declared ones are github,"
        " semgrep."
    )


def test_a_role_naming_a_server_where_none_is_declared_says_where_to_declare_it() -> None:
    with pytest.raises(ConfigError) as raised:
        check_role_servers({}, {"coder": role({"server": "github"})})
    assert str(raised.value) == (
        "roles.coder.tools.mcp: github isn't a declared MCP server; declare it in an"
        " [mcp.<name>] table of .orbit/config.toml."
    )


def test_a_personal_role_naming_an_undeclared_server_is_the_user_configs_fault() -> None:
    with pytest.raises(UserConfigError, match=r"^roles\.helper\.tools\.mcp: nope isn't"):
        check_role_servers(SERVERS, {"helper": role("nope")}, users={"helper"})


def test_the_projects_undeclared_servers_are_refused_ahead_of_the_users() -> None:
    roles = {"helper": role("nope"), "coder": role("other")}
    with pytest.raises(ConfigError) as raised:
        check_role_servers(SERVERS, roles, users={"helper"})
    assert not isinstance(raised.value, UserConfigError)
    assert str(raised.value).startswith("roles.coder.tools.mcp: other isn't")


# Approving a server's tools


def approving(approve: object) -> dict[str, McpServer]:
    """The servers, github holding the calls ``approve`` says for approval."""
    return {**SERVERS, "github": McpServer.model_validate({**GITHUB, "approve": approve})}


def test_approving_a_tool_no_role_allowing_the_server_lists_is_refused() -> None:
    roles = {"coder": role({"server": "github", "tools": ["get_issue"]})}
    with pytest.raises(ConfigError) as raised:
        check_role_servers(approving(["create_issue"]), roles)
    assert str(raised.value) == (
        "mcp.github.approve: create_issue isn't a tool any role allowing github lists; they list"
        " get_issue."
    )


def test_approving_a_tool_one_of_the_roles_allowing_the_server_lists_passes() -> None:
    roles = {
        "coder": role({"server": "github", "tools": ["get_issue"]}),
        "planner": role({"server": "github", "tools": ["get_issue", "create_issue"]}),
    }
    check_role_servers(approving(["create_issue", "get_issue"]), roles)


@pytest.mark.parametrize(
    ("approve", "roles"),
    [
        (
            ["create_issue"],
            {"coder": role({"server": "github", "tools": ["x"]}), "p": role("github")},
        ),
        (["create_issue"], {"coder": role("semgrep")}),
        (True, {"coder": role({"server": "github", "tools": ["get_issue"]})}),
    ],
    ids=["a role allows all its tools", "no role allows it", "every call"],
)
def test_approving_tools_any_role_might_call_passes(
    approve: object, roles: dict[str, Role]
) -> None:
    check_role_servers(approving(approve), roles)
