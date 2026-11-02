"""The rules of proxying external MCP servers' tools: the names agents see them by, which a role
allows, the variables a server's environment is filled from, which calls wait for the user's
approval, and the wording of refusals and notices."""

import hashlib

import pytest

from orbit.agent.domain.mcp_proxy import (
    HASH_LENGTH,
    NAME_LIMIT,
    FilledServer,
    ProxiedTool,
    RefusedCredentials,
    UnsetVariable,
    UpstreamTool,
    call_failure,
    declined,
    failure_notice,
    filled_env,
    filled_server,
    find_proxied,
    grant_for,
    is_proxied,
    missing_tool_notices,
    needs_approval,
    proxied_name,
    proxied_tools,
    refusal,
    server_variables,
    variables_in,
)
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.domain.roles import Role, RoleTools

SCHEMA = {"type": "object", "properties": {"number": {"type": "integer"}}}
GET_ISSUE = UpstreamTool("get_issue", "Get an issue.", SCHEMA)
CREATE_ISSUE = UpstreamTool("create_issue", None, {"type": "object"})


def role(*mcp: object) -> Role:
    """A task role whose agents may use the MCP servers ``mcp``, as a file gives them."""
    return Role(instructions="Work.", tools=RoleTools.model_validate({"mcp": mcp}))


# Names


@pytest.mark.parametrize(
    ("server", "tool", "name"),
    [
        ("github", "get_issue", "github__get_issue"),
        ("github", "get issue!", "github__get_issue_"),
        ("files", "read.file-v2", "files__read.file-v2"),
        ("files", "lire_fiché", "files__lire_fich_"),
    ],
)
def test_a_proxied_name_joins_the_server_and_the_tool_with_unsafe_characters_replaced(
    server: str, tool: str, name: str
) -> None:
    assert proxied_name(server, tool) == name


def test_an_over_long_proxied_name_is_cut_to_the_limit_and_ends_in_a_hash() -> None:
    name = proxied_name("github", "t" * 200)
    assert len(name) == NAME_LIMIT
    assert name.startswith("github__ttt")
    assert name != proxied_name("github", "t" * 201)
    assert name == proxied_name("github", "t" * 200)


def test_a_name_at_the_limit_is_kept_whole() -> None:
    tool = "t" * (NAME_LIMIT - len("github__"))
    assert proxied_name("github", tool) == f"github__{tool}"


@pytest.mark.parametrize(
    ("name", "proxied"),
    [("github__get_issue", True), ("get_task", False), ("__x", True), ("plain", False)],
)
def test_a_name_with_a_double_underscore_is_proxied(name: str, proxied: bool) -> None:
    assert is_proxied(name) is proxied


# What a role allows


def test_a_role_allowing_a_server_whole_gets_every_tool_it_offers() -> None:
    grant = grant_for(role("github"), "github")
    assert grant is not None
    tools = proxied_tools("github", grant, (GET_ISSUE, CREATE_ISSUE))
    assert tools == (
        ProxiedTool("github__get_issue", "github", GET_ISSUE),
        ProxiedTool("github__create_issue", "github", CREATE_ISSUE),
    )


def test_a_role_listing_tools_gets_only_those_the_server_offers() -> None:
    grant = grant_for(role({"server": "github", "tools": ["get_issue", "create_isue"]}), "github")
    assert grant is not None
    assert proxied_tools("github", grant, (GET_ISSUE, CREATE_ISSUE)) == (
        ProxiedTool("github__get_issue", "github", GET_ISSUE),
    )


def test_a_role_not_naming_a_server_has_no_grant_of_it() -> None:
    assert grant_for(role("github"), "semgrep") is None


def test_a_proxied_tool_is_found_by_its_proxied_name() -> None:
    tools = (ProxiedTool("github__get_issue_", "github", UpstreamTool("get issue!", None, {})),)
    assert find_proxied(tools, "github__get_issue_") == tools[0]
    assert find_proxied(tools, "github__get_issue") is None


def hashed(name: str, whole: str) -> str:
    """``name`` ending in the short hash of ``whole``, as a colliding name is given."""
    return f"{name}-{hashlib.sha256(whole.encode()).hexdigest()[:HASH_LENGTH]}"


def test_an_unsafe_tool_colliding_with_a_safe_one_ends_in_a_hash_and_the_safe_one_doesnt() -> None:
    odd = UpstreamTool("get issue", None, {})
    grant = grant_for(role("github"), "github")
    assert grant is not None
    tools = proxied_tools("github", grant, (odd, GET_ISSUE))
    assert tools == (
        ProxiedTool(hashed("github__get_issue", "github__get issue"), "github", odd),
        ProxiedTool("github__get_issue", "github", GET_ISSUE),
    )
    assert find_proxied(tools, tools[0].name) == tools[0]
    assert find_proxied(tools, "github__get_issue") == tools[1]


def test_unsafe_tools_colliding_with_each_other_each_end_in_their_own_hash() -> None:
    first = UpstreamTool("get issue", None, {})
    second = UpstreamTool("get/issue", None, {})
    grant = grant_for(role("github"), "github")
    assert grant is not None
    tools = proxied_tools("github", grant, (first, second))
    assert [tool.name for tool in tools] == [
        hashed("github__get_issue", "github__get issue"),
        hashed("github__get_issue", "github__get/issue"),
    ]
    assert find_proxied(tools, tools[1].name) == tools[1]


def test_a_tool_keeps_its_hashed_name_when_the_role_doesnt_list_the_one_it_collides_with() -> None:
    odd = UpstreamTool("get issue", None, {})
    grant = grant_for(role({"server": "github", "tools": ["get issue"]}), "github")
    assert grant is not None
    assert proxied_tools("github", grant, (odd, GET_ISSUE)) == (
        ProxiedTool(hashed("github__get_issue", "github__get issue"), "github", odd),
    )


def test_an_over_long_colliding_name_is_cut_to_the_limit() -> None:
    long = "t" * (NAME_LIMIT - len("github__") - 1) + "_"
    odd = UpstreamTool(f"{long[:-1]} ", None, {})
    grant = grant_for(role("github"), "github")
    assert grant is not None
    tools = proxied_tools("github", grant, (UpstreamTool(long, None, {}), odd))
    assert tools[0].name == f"github__{long}"
    assert len(tools[1].name) == NAME_LIMIT
    assert tools[1].name != tools[0].name


def test_each_tool_a_role_names_that_the_server_lacks_gives_one_notice() -> None:
    roles = {
        "coder": role({"server": "github", "tools": ["get_issue", "create_isue"]}),
        "planner": role("github"),
        "reviewer": role({"server": "semgrep", "tools": ["scan"]}),
        "tester": role({"server": "github", "tools": ["list_prs"]}),
    }
    assert missing_tool_notices("github", roles, (GET_ISSUE, CREATE_ISSUE)) == (
        "github offers no tool create_isue, which roles.coder.tools.mcp names.",
        "github offers no tool list_prs, which roles.tester.tools.mcp names.",
    )


# The environment


def test_the_variables_an_environment_references_are_found_in_both_forms() -> None:
    env = {"TOKEN": "$GITHUB_TOKEN", "URL": "https://${HOST}/api/$HOST", "PLAIN": "x"}
    assert variables_in(env) == ("GITHUB_TOKEN", "HOST")


def test_an_environment_is_filled_from_the_values_of_its_variables() -> None:
    env = {"TOKEN": "$GITHUB_TOKEN", "URL": "https://${HOST}/api", "PLAIN": "x"}
    values = {"GITHUB_TOKEN": "secret", "HOST": "example.com"}
    assert filled_env(env, values) == {
        "TOKEN": "secret",
        "URL": "https://example.com/api",
        "PLAIN": "x",
    }


def test_an_environment_missing_a_variable_names_it() -> None:
    unset = filled_env({"TOKEN": "$GITHUB_TOKEN"}, {})
    assert unset == UnsetVariable("GITHUB_TOKEN")
    assert isinstance(unset, UnsetVariable)
    assert unset.message == "GITHUB_TOKEN isn't set in orbit's environment or credentials.toml."


SEMGREP = McpServer(
    url="https://semgrep.example.com/mcp",
    env={"MODE": "$MODE"},
    headers={"Authorization": "Bearer ${SEMGREP_TOKEN}", "X-Mode": "$MODE"},
)


def test_a_servers_variables_are_those_its_env_and_headers_reference_each_once() -> None:
    assert server_variables(SEMGREP) == ("MODE", "SEMGREP_TOKEN")


def test_a_servers_env_and_headers_are_filled_from_the_values_of_its_variables() -> None:
    filled = filled_server(SEMGREP, {"SEMGREP_TOKEN": "secret", "MODE": "ci"})
    assert filled == FilledServer(
        env={"MODE": "ci"}, headers={"Authorization": "Bearer secret", "X-Mode": "ci"}
    )


@pytest.mark.parametrize(
    ("values", "unset"), [({"MODE": "ci"}, "SEMGREP_TOKEN"), ({"SEMGREP_TOKEN": "s"}, "MODE")]
)
def test_a_server_missing_a_variable_names_it(values: dict[str, str], unset: str) -> None:
    assert filled_server(SEMGREP, values) == UnsetVariable(unset)


# Wording


# Approval


@pytest.mark.parametrize(
    ("approve", "tool", "held"),
    [
        (False, "create_issue", False),
        (True, "create_issue", True),
        (True, "get_issue", True),
        (("create_issue",), "create_issue", True),
        (("create_issue",), "get_issue", False),
        ((), "create_issue", False),
    ],
)
def test_a_call_waits_for_approval_when_its_server_approves_all_or_lists_the_tool(
    approve: bool | tuple[str, ...], tool: str, held: bool
) -> None:
    server = McpServer(command=("github-mcp-server",), approve=approve)
    assert needs_approval(server, tool) is held


def test_a_declined_call_names_the_tool() -> None:
    assert declined("github__create_issue") == "The user declined github__create_issue."


def test_a_refusal_names_the_role_and_the_tool() -> None:
    assert refusal("coder", "github__create_issue") == (
        "roles.coder doesn't allow github__create_issue."
    )


def test_a_failure_notice_names_the_server_and_why() -> None:
    assert failure_notice("github", "it exited") == (
        "MCP server github failed: it exited. Agents' calls to it return errors until it starts."
    )


def test_a_failed_call_names_the_server_and_why() -> None:
    assert call_failure("github", "it exited") == "MCP server github failed: it exited."


def test_a_server_refusing_orbits_credentials_says_so() -> None:
    assert call_failure("semgrep", RefusedCredentials(401)) == (
        "MCP server semgrep refused orbit's credentials (HTTP 401)."
    )
    assert failure_notice("semgrep", RefusedCredentials(403)) == (
        "MCP server semgrep refused orbit's credentials (HTTP 403). Agents' calls to it return"
        " errors until it starts."
    )


def test_a_reason_ending_a_sentence_doesnt_end_it_twice() -> None:
    assert (
        call_failure("github", "No such program.") == "MCP server github failed: No such program."
    )
    assert failure_notice("github", "No such program.").startswith(
        "MCP server github failed: No such program. Agents'"
    )
