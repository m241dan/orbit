# Serve and connect requirements

The decisions and plan for phase 3a of milestone 3 (control-plane server and remote client) of the control-plane design: `orbit serve` runs the control plane on its own and serves the protocol over a WebSocket on loopback; `orbit chat` connects to it, finds it on its own, or runs in-process; many frontends share one session; and whichever orbit reaches a repository first owns it.

## Goal

The control plane outlives its frontends: agents keep working with no TUI open, any number of TUIs can watch and drive it, and a TUI that loses the connection catches up from the event log. Two orbits never both run work on one repository.

## Scope

1. Milestone 3 ships as four phases: 3a serve and connect on loopback (this record); 3b `Attach`, the session lifecycle and the always-on picker; 3c TLS and mTLS, binds beyond loopback, `wss://` for `--connect` and named remotes, with the README's certificate sections; 3d `/healthz` and `/readyz`, the container files and the README's hosting-in-a-container section. Over three phases: health and container files would weigh down the TLS plan; over two: a large local plan.
2. Settled for 3b: every frontend starts with the session picker: "Resume <active session>" first and preselected, then the other saved sessions, then "New session"; with no saved sessions, only "New session"; `--new`/`--name` skip it; in-process `orbit chat` the same without "Resume".

## The server

3. `orbit serve -C <path>` takes today's backend flags (`--model`, `--tools`, `--verify`, `--worktree`, `--review`, `--usage`, `--new`, `--name`, `--profile`) plus `--state-dir`, `--config-dir`, `--host` (default 127.0.0.1) and `--port` (default 7433; 0 lets the OS choose).
4. `[server]` in the user config sets `host`, `port` and `max_message_mb`; flags override it. A port in use exits with status 2 naming the port, `--port` and the config file. In 3a a host other than loopback is refused at startup, saying TLS comes later. Over a default of 0: no known port for remote setups; over `[server]` in 3c: service managers couldn't fix the port.
5. `orbit serve` opens the workspace's most recently active session, or a new unsaved one when none is saved; `--new`/`--name` start a new one. Over always new: restarts scatter work; over none until a frontend picks: work couldn't run with no frontend.
6. The server is starlette's WebSocket in `orbit.agent.external`, served by uvicorn, JSON frames with phase 1d's `encode`/`decode`. `websockets` is the one new dependency, for uvicorn's server side and the client. Over `wsproto`: two libraries for one protocol; over an httpx add-on: a smaller project, and the server still needs one.
7. Everything phase 2b logs goes to every connection, outcomes included; session-less events to every connection; `CONNECTION_EVENTS` only to the connection that asked; the TUI shows a `CommandRefused` only for commands it sent. Over outcomes to the sender only: live and replayed views differ, or 2b's logging is undone.
8. Stopping (first ctrl+c or SIGTERM): stop accepting connections, cancel work (stopping agents and revoking their tokens), flush the event log, send each frontend a close frame with a reason, remove the `run/` file, exit; a second ctrl+c exits at once. Over waiting for the task: a stop could take an hour; over refusing: service managers couldn't stop it.
9. Per connection: messages up to 64 MiB both ways (`max_message_mb`), the server's limit, enforced by the server on what it accepts and sends while the client takes any size, a larger one closing the connection with a reason (over unlimited: memory exhaustion; over 1 MiB with continuation: phase 1b ruled out multiplexing). A send queue bounded at 10,000 events, overflow closing the connection as "too far behind" and the client catching up with `since` (over unbounded: memory grows; over blocking: one stalled frontend freezes every agent). A frame that can't be decoded but carries a `command_id` is answered `CommandRefused` (`invalid`, or `unknown` for an unknown type); one without is closed with code 1008 (over always closing: one typo drops a script; over ignoring: clients never learn).
10. `orbit serve` prints one line at start (repository, URL, token file path, owner or read-only), one per frontend connecting or disconnecting (client name, address), and one at shutdown; warnings and errors go to stderr through stdlib `logging` at WARNING and above. Nothing more; the rest waits for the telemetry milestone. Over silence: no sign it's up; over every event: that's telemetry's.

## The token

11. 32 random bytes (`secrets.token_urlsafe`), created by `orbit serve` on first start at `<config dir>/token` (0600, in an owner-only directory) and reused after. A token file others can read is refused, naming the file and the `chmod` fix. Sent as `Authorization: Bearer` on the upgrade and checked with `hmac.compare_digest`; missing or wrong gets HTTP 401 before the WebSocket opens, and the client names the token file it used. It grants full control of the control plane. `orbit token rotate` replaces it atomically; new connections need the new token, open ones stay (restart the server to drop them). Never printed, logged or put in the `run/` file. A client on another machine gets a copy over a trusted channel such as `scp` (in 3a through an SSH tunnel; 3c adds TLS and mTLS). Over a new token each start: remote clients re-copy after every restart; over a user-supplied file: a setup step before the first run.

## Connecting

12. `orbit.client`, a new package: a `Session` over `websockets` importing only `orbit.protocol`. Contracts: it imports nothing else of orbit's, and `orbit.agent`, `orbit.tui` and `orbit.protocol` never import it. `orbit.cli` builds it.
13. `orbit chat --connect <url>` (`ws://` on loopback in 3a) uses the token from `<config dir>/token` by default, or `--token-file <path>`, and skips discovery. Over only the default file: connecting to another account's server means overwriting your token; over `--token <value>`: shell history and process listings show it.
14. Without `--connect`, `orbit chat` reads `<state dir>/run/<workspace id>.json`, written owner-only by `orbit serve` with its URL and process id and removed on exit, and connects if the process is alive and the token is accepted; otherwise it runs in-process. `--local` forces in-process. Over a fixed port: it can't tell which repository a server is for; over manual only: contradicts the design.
15. Connecting with a backend flag (`--model`, `--tools`, `--verify`, `--worktree`, `--review`, `--usage`, `--profile`) exits with status 2 saying they belong on `orbit serve`; `--new`/`--name` send `NewSession` after joining. Over ignoring them with a Notice: the user may think they took effect; over applying what can be: one frontend would change a shared session.
16. A connected `orbit chat` shows the open session with no picker in 3a; ctrl+p switches it for every viewer, as today. Quitting the TUI disconnects and leaves the server running.
17. Losing the connection: the client reconnects with backoff (0.5 s doubling to 10 s, with jitter) and `Hello(since=<last seq>)`, ping every 20 s with a 20 s timeout; the TUI shows "Reconnecting to <url>…" and refuses commands locally with "Not connected"; after 2 minutes a modal offers "Keep trying" or "Quit". Over retrying forever: the user may wait on a dead server; over exiting: every blip ends the TUI.
18. A close frame saying the server is shutting down opens a modal at once, "The control plane stopped", with "Keep trying" (preselected) and "Quit", while reconnecting continues behind it. Over treating it as a lost connection: the user learns two minutes late; over exiting: a restart means reopening every TUI.

## Ownership

19. The first orbit to reach a repository, `orbit serve` or in-process `orbit chat`, owns it through an exclusive `flock` on `.git/orbit/workspace.lock` (`.orbit/workspace.lock` outside git), holding its process id and a server's URL. Over a row in the store: stale after a crash and invisible across machines; over the `run/` file: in-process chats don't write one.
20. A second arrival is read-only: a Notice naming the owner, a persistent "read-only" mark by the prompt, `read_only` on the Catalog and Snapshot; work commands (`RunTasks`, triggers) are accepted and queued; the planner, sessions and drafts work as usual.
21. A read-only orbit retries the lock every few seconds; when it's free, a modal offers "Take over" or "Stay read-only". Taking over runs the queued work, with a Notice; the queue is dropped on exit. Over staying read-only until restart: no way to take over without one; over taking over automatically: the mode would change under the user.

## Docs

22. README: `orbit serve`, `orbit chat --connect`, `--token-file`, `--local`, discovery, the token (what it grants, copying it to another machine, rotation, never sharing it), ownership and read-only, `[server]`; Architecture gains `orbit.client` and the server adapter. `docs/protocol.md` gains the transport: auth on the upgrade, close codes and reasons, the message limit and slow clients. The schema gains `read_only`.

## How it will be built

1. Write this record.
2. Let several frontends share one session: `AgentSession.connect()` returning a `SessionConnection` (a `Session` with its own handshake and stream); fan-out as in 7; bounded send queues; `ThreadedSession` wrapping one connection.
3. Serve the control plane on loopback: `orbit serve` and `[server]`; `TokenFile`; `ControlPlaneServer` (starlette WebSocket, one `SessionConnection` per WebSocket, bad frames, size limit); `RunFile`; shutdown; printed lines.
4. Connect `orbit chat` to a control plane: `orbit.client.WebSocketSession` with reconnect and `since`; `--connect` and `--token-file`; backend flags refused; the TUI's reconnecting status, 2-minute modal and stopped modal.
5. Discover a running control plane: `discover_control_plane` reading the `run/` file; `--local`.
6. Hold the repository with a workspace lock: `WorkspaceLock`; read-only mode with queued work; `read_only` on the Catalog and Snapshot; the TUI's mark.
7. Take over a freed repository: `OwnershipOffered`, `TakeOwnership`, the TUI's modal, running the queue.
8. Add `orbit token rotate`.

## Deferred

A continuation scheme so WebSocket messages can stay small; identities per person on a shared control plane.
