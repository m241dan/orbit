# Many workspaces requirements

The decisions and plan for phase 4a of milestone 4 (workspaces) of the control-plane design: one control plane serves many repositories at once, each with its own open session, work lane and lock; a server isn't tied to the directory it starts in; and frontends choose a workspace, then a session.

## Goal

`orbit serve` is a host for workspaces: any frontend can add, remove and switch between the repositories it serves, each running its own work. A local `orbit chat` stays the simple one-repository mode it is today.

## Scope

1. Milestone 4 ships as four phases: 4a many workspaces (this record); 4b `AddWorkspace` by URL, re-attaching by fingerprint and `RemoveWorkspace --purge`; 4c the control-plane stream and the coder cap, with unloading idle sessions; 4d the TUI's file-explorer workspace picker. Over three phases: 4a is already the largest; over two: a very large backend plan.

## Shape

2. A new `ControlPlane` (application layer) holds one `WorkspaceHost` per served workspace (today's `AgentSession`, renamed and scoped to one workspace), routes commands by the envelope's `workspace`, merges every host's events into the connections, and owns what's shared: the connections, the store connection, the `seq` counter and the usage monitor. Over one session object for many workspaces: rewriting the most tested code; over a process per workspace: process management and cross-process limits.

## Which workspaces are served

3. A server serves the workspaces flagged as served in the registry. `AddWorkspace` and `orbit serve --add <path>` (repeatable) set the flag; `RemoveWorkspace` clears it. The directory `orbit serve` starts in means nothing; a server with none served starts empty. `-C`, `--new` and `--name` leave `orbit serve`: sessions are chosen in the picker. Over serving the starting directory when nothing is flagged: the directory would matter again; over always serving it: a server tied to where it started.
4. At start, each served workspace opens its most recently active session (or a new unsaved one), takes its workspace lock (read-only and the take-over offer if another orbit holds it, per phase 3a), and runs its startup check.
5. `AddWorkspace(path)`: an absolute path on the server host to an existing directory, a repository's top level or a plain directory; a subdirectory resolves to its repository's top level; a missing path is refused with `unknown`, a file with `invalid`; an already-served workspace is a no-op. Adding registers it (or finds it by path), flags it, takes its lock, sends a new Catalog and runs its startup check. Over git repositories only: narrows what orbit supports; over creating missing directories: typos make junk.
6. `RemoveWorkspace(workspace)` is refused with `busy` while it runs work or another frontend views one of its sessions; otherwise it releases the lock, clears the flag, keeps the records, sends a new Catalog, and detaches the requester if it was there, returning its frontend to the workspace picker. Over removing at any time: it can kill a watched task; over detaching other viewers: views vanish.
7. A local `orbit chat` serves exactly its own repository and attaches straight to it; `AddWorkspace` and `RemoveWorkspace` are refused there with `invalid`, pointing to `orbit serve`. Over local adding: a local orbit would run other repositories' work.
8. One `orbit serve` per account and machine: discovery reads `<state dir>/run/server.json`, replacing the per-workspace run file; a second server exits with status 2 naming the running one and `--state-dir` for a separate store; a stale file is replaced. Over several servers: they'd fight over the same flagged workspaces.

## Protocol version 2

9. `Hello` attaches nothing: `Welcome`, `Catalog`, then nothing until `Attach(workspace, session)`, which answers with the Snapshot. This drops phase 1c's "Snapshot after Catalog" rule, so `PROTOCOL_VERSION` becomes 2, and `OpenSession`, `ListSessions` and `SessionsListed` are removed, as phase 3b's guard test requires. Over attaching when possible: two openings to handle and an implicit choice of workspace; over keeping auto-attach: empty servers impossible.
10. Envelopes carry `workspace: str | None`: the registry's id for session commands and events, `None` for control-plane messages (adding, removing, listing workspaces, browsing, the phase 4c stream). `"default"` and `DEFAULT_WORKSPACE` are removed. A command for a workspace other than the attached one is refused with `wrong_session` naming both; an unknown id with `unknown`. Over keeping `"default"` as an alias: version 2 breaks old clients anyway; over paths: they change when a repository moves; over a new `unknown_workspace` code: clients treat `unknown` the same.
11. The Catalog lists every served workspace (`WorkspaceEntry`: id, label, path for display, `read_only`, sessions with their state); `read_only` is per workspace. Sessions keep phase 3b's rules within their workspace: one open session per workspace, `ActivateSession`, read-only views.

## Frontends

12. Connected, the TUI opens with a workspace picker, a list modal reopened with **ctrl+w**: "Add <this repository>" first when `orbit chat` runs in a repository the server doesn't serve; the served workspaces with label, path, active session and state, the current repository preselected; "Add a path…", a text field for a server-side path; **x** removes after confirmation. Choosing a workspace opens its session picker ("Resume <active>" first). Phase 4d replaces "Add a path…" with the explorer. Over no picker until 4d: no switching workspaces from the TUI; over no remove key: removing needs the command line.
13. `orbit chat` in a repository the server doesn't serve still connects, and the picker's first entry adds it; `--local` runs in-process. Over running in-process automatically: the server and local orbit show different workspaces; over a prompt before the TUI: a prompt outside the TUI.

## Health and docs

14. `/readyz`: pass once the store is open and listening; fail with `starting`, `shutting_down` or `storage_halted`; warn with `read_only` when any served workspace is read-only. `orbit serve`'s start line lists the served workspaces, each owner or read-only, or says none yet. Over a per-workspace probe parameter: probes don't know ids.
15. The schema is regenerated for version 2; `docs/protocol.md` covers the new handshake, `Attach(workspace, session)`, the workspace field, `AddWorkspace`/`RemoveWorkspace` and the removed messages; README covers `orbit serve --add`, servers without a directory, the workspace picker, ctrl+w, and one server per account.

## How it will be built

1. Write this record.
2. Put a control plane over the workspace backend: `AgentSession` becomes `WorkspaceHost`; `ControlPlane` owns connections, `seq`, the usage monitor and routing; still one workspace.
3. Share one store across workspaces: `SqliteStore` opened once, `SqliteStore.workspace_view` per host, `WorkspaceRegistry` (find or register by path, the `served` flag in migration 5).
4. Move the protocol to version 2: `Hello` attaching nothing, `Attach(workspace, session)`, `workspace: str | None`, the removals, the TUI attaching on its own until the picker.
5. Serve the flagged workspaces: a `WorkspaceHost` per served workspace, `orbit serve --add`, `-C`/`--new`/`--name` removed from `orbit serve`, `run/server.json`, the start line, `/readyz`.
6. Add and remove workspaces from a frontend: `AddWorkspace`, `RemoveWorkspace`, refused on a local orbit.
7. Pick a workspace in the TUI: the workspace picker and ctrl+w.
