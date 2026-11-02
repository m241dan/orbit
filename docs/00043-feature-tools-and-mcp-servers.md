# Tools and MCP servers requirements

The decisions and plan for milestone 13 of `scratch/control-plane-design.md` (*Tools and MCP
servers*), after `docs/00042-feature-lens-reviews-and-schedules.md`: workspace levels and web
access replace tool access, roles' tools move into one table, and orbit proxies external MCP
servers to the agents whose roles allow them.

## Goal

A role's `[roles.<name>.tools]` says what its agents may use. `orbit` lists orbit's tools,
`workspace` gives one of four levels (`none`, `read`, `write`, `shell`), `web` turns web access
on or off, and `mcp` names external MCP servers. Orbit connects to those servers itself and
exposes the allowed tools as its own, so every runtime is held to the same allow list and every
call is traced. Calls can be held for the user's approval.

## Tools and levels

1. One plan and one record for the whole milestone: levels and web first, then MCP servers;
   over two phases, 13a and 13b (13b's questions would wait).
2. `[roles.<name>.tools]` holds `orbit` (orbit's tools), `workspace` (`none`, `read`, `write`
   or `shell`), `web` and `mcp`. The old `tool_access` and list-form `tools` are a load error
   showing the new form, and every config that stands today is rewritten in the same task; over
   accepting them for a version (with a notice) and over accepting them forever.
3. `Settings` gains additive `workspace_level` and `web`. `tool_access` stays on the wire for
   older peers, mapped both ways. `--tools` takes the four levels and `--web`/`--no-web` set
   web; over dropping per-task and per-session access, and over keeping it for conversations
   only.
4. Shipped roles keep web wherever it's effectively on today, every role but `none`; over web
   only for the planner, experimenter and coder (reviewers would lose WebSearch).

## MCP servers

5. Servers are declared in `.orbit/config.toml` as `[mcp.<name>]`. The user layer may override
   a declared server's `command`, `url`, `env` and `headers`, but not add servers; over
   project-only (machine paths committed) and user-added servers (roles would fail on other
   machines).
6. Credentials come from orbit's environment, else `credentials.toml` in orbit's config
   directory, which must be mode 0600; over the environment only, and the OS keyring.
7. A failing upstream server returns errors to the calling agent, retries on the next call, and
   gives one notice per failure streak; over span-only (a broken server goes unnoticed) and
   failing the task (one flaky server kills work).
8. `approve = true` on a server, or a list of its tools, holds each such call for the user's
   decision in the TUI, as consultation approvals are held; over the allow list as the only
   approval.
9. A shared stdio server runs in a neutral directory in orbit's state directory; with
   `shared = false` it runs in the agent's own workspace; over the user's checkout (agents could
   edit it through a server) and always the agent's workspace (no sharing).

## Edge cases

10. 1. A config file sets `tool_access`, or `tools` as a list: a load error showing the new
       form, as in `roles.coder.tool_access is gone; write [roles.coder.tools] workspace =
       "shell", web = true.`
    2. A saved session or task carries `tool_access`: mapped on read, `off` → `none` without
       web, `restricted` → `read` with web, `full` → `shell` with web.
    3. An older peer sends `Settings` with only `tool_access`: mapped as in 2. Orbit also sends
       `tool_access` for older peers: `none` → `off`, `read` → `restricted`, `write` and `shell`
       → `full`.
    4. A role asks for a level its runtime can't enforce: a load error naming the role, the
       runtime and the levels it supports.
    5. `workspace = "none"` with `web = true`, or with `mcp` servers: allowed; web and MCP are
       separate sources.
    6. A role names an MCP server that isn't declared: a load error naming the role and the
       server.
    7. A server named like an orbit tool, or with `__` in its name: a load error.
    8. The user layer overrides a server the project doesn't declare: a load error; the user
       layer can't add servers.
    9. A role's MCP allow list names a tool the server doesn't offer: on first connection, the
       notice `github offers no tool create_isue, which roles.coder.tools.mcp names.` Agents
       never see that tool.
    10. An agent calls a proxied tool its role doesn't allow: a coded error to the agent,
        recorded on its span.
    11. A server fails to start, crashes, times out (`timeout_seconds`, default 300) or is
        unreachable: the call returns an error with the reason, the next call tries a fresh
        start, and there's one notice per failure streak.
    12. A connection idle for `idle_minutes` (default 10): closed; the next call reconnects.
    13. The workspace closes, or orbit exits: connections close, stdio servers are stopped
        (terminated, then killed), and in-flight calls are answered with an error.
    14. An agent's run ends mid-call: the upstream call is cancelled, and its `shared = false`
        server is stopped.
    15. A variable a server names is in neither the environment nor `credentials.toml`: the
        server's start fails, `GITHUB_TOKEN isn't set in orbit's environment or
        credentials.toml.` Only the variables a server names are needed, and only when it
        starts.
    16. `credentials.toml` is readable by group or others: refused, with `<path> must be
        private: chmod 600 it.` The servers that need it fail to start.
    17. An agent calls an `approve` tool: the call is held, and the TUI asks the user (agent,
        role, server, tool, arguments). Allow runs it; deny returns `The user declined
        github__create_issue.`
    18. A held call has no frontend connected for an hour: it expires as declined.
    19. The agent stops, or is cancelled, while its call is held: the approval is withdrawn.
    20. `approve` names a tool the role's list doesn't allow: a load error.
    21. MCP config changes while the workspace is open: it takes effect when the workspace next
        opens.
    22. Tasks are verified unless their level is `none` (before: unless `off`).
    23. `--tools` takes `none`, `read`, `write` or `shell`, and `--web`/`--no-web` set web. The
        TUI's label reads e.g. `read + web`.

## Decided for the user

11. `WorkspaceLevel = Literal["none", "read", "write", "shell"]` is in `orbit.protocol`.
    `Capabilities.tool_access` becomes `workspace_levels`, plus `web: bool`. Claude and Kiro
    declare all four levels and web.
12. Claude adapter mapping:
    - `none`: `--tools ""`, or only `WebSearch,WebFetch` with web.
    - `read`: today's `--restricted` and read-only tools.
    - `write`: read tools plus `Edit`, `Write` and `NotebookEdit` under
      `--permission-mode acceptEdits`, with Bash and its kin disallowed.
    - `shell`: `bypassPermissions`.
    - Web off denies `WebSearch` and `WebFetch`.

    Kiro maps the levels and web to its built-ins list. Translation stays in
    `orbit.agent.external`; the levels' meaning is Domain.
13. `[mcp.<name>]`: `command` (a list) or `url`, exactly one; `env` and `headers` tables naming
    variables; `shared = true`; `idle_minutes = 10`; `timeout_seconds = 300`; `approve`
    (`true`, or a list of tool names). `tools.mcp` is a list of `{ server, tools }`, where
    omitting `tools` allows all of the server's tools.
14. Proxying uses `MCPServer`'s public `middleware=` hook, keeping orbit's decorated tools (see
    report 4). Per agent token, an agent's `tools/list` gets its allowed proxied tools appended,
    named `<server>__<tool>` with their upstream schemas, and loses orbit tools its role lacks.
    `tools/call` to a proxied name goes to the proxy; a hidden name is refused with `is_error`
    set; anything else passes through. Upstream names outside `[A-Za-z0-9._-]`, or past 128
    characters combined, are sanitized, with a map back. The hook is provisional in mcp 2.x, so
    its use stays in `mcp_host.py`, with a test running the real server and client. Upstream
    clients (stdio and streamable HTTP) live in `orbit.agent.external`; connections are owned
    per workspace, through `orbit.execution` helpers.
15. One span per proxied call, with `orbit.mcp.server`, `orbit.mcp.tool`, the outcome and any
    error, never arguments or results; approvals add `orbit.mcp.approval` (`allowed`,
    `declined`, `expired` or `withdrawn`).
16. Held calls join the approvals the TUI shows for consultations, through additive protocol
    models `ToolCallHeld`, `ToolCallDecided` and the command `DecideToolCall`. A declined call's
    error is `The user declined github__create_issue.`
17. `credentials.toml` is a flat table of `NAME = "value"`. The environment wins over it. It's
    read when a server starts, never logged, and never put in an agent's environment.
18. The README, `docs/protocol.md` and `docs/telemetry.md` change in the task whose behaviour
    they describe.

## How it will be built

1. Write this record.
2. Replace tool access with workspace levels and web: `WorkspaceLevel`,
   `Settings.workspace_level` and `Settings.web` with the `tool_access` mapping,
   `Capabilities.workspace_levels` and `web`, the Claude and Kiro translation, `--tools` and
   `--web`, the TUI label, the verification rule, the session format bump; role config still
   reads `tool_access`, mapped.
3. Move role tools into the `[roles.<name>.tools]` table: `orbit`, `workspace`, `web`; the load
   error for the old forms; every standing config rewritten.
4. Declare MCP servers: `[mcp.<name>]`, `tools.mcp`, user-layer overrides, validation, the
   reference config and `config show`; nothing connects yet.
5. Proxy stdio MCP servers to agents: the middleware's per-agent listing and dispatch, the
   upstream stdio client, lazy shared connections, idle close, `shared = false`, the neutral
   directory, failure notices, spans, shutdown, credentials from the environment.
6. Proxy URL MCP servers: the streamable HTTP client and `headers`.
7. Read credentials from `credentials.toml`: the file, the 0600 check, the environment first.
8. Hold approved MCP calls for the user: `approve`, `ToolCallHeld`, `ToolCallDecided` and
   `DecideToolCall`, the TUI approvals, expiry and withdrawal, the configs updated.

## Deferred

- Proxied MCP for the built-in harness, in-process (milestone 15).
- `tools/list_changed` notifications: an agent's tools are fixed for its run.
