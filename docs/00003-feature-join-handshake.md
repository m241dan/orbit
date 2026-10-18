# Join handshake requirements

The decisions and plan for phase 1c of milestone 1 (generic protocol) of the control-plane design: a frontend opens with `Hello`, and the backend answers with `Welcome`, a `Catalog` of what exists and a `Snapshot` of the open session, so a frontend can join at any time without replaying history.

## Goal

Everything a frontend shows can be learned from the start of a stream: `Welcome`, `Catalog`, `Snapshot`, then live events. Switching sessions works the way joining does. No user-visible change.

## Handshake

1. The handshake lands in milestone 1, in-process too (settled with phase 1a).
2. `Hello` is a command in the `Command` union, sent in a `CommandEnvelope`; its outcome is `Welcome` (carrying Hello's `command_id`) in place of `CommandAccepted`, or a `CommandRefused`. Over a separate `Session.hello` method: WebSocket clients would need a different path; over an implicit handshake: the server milestone would build it from scratch.
3. `Hello(client: str, protocol_version: int, since: int | None = None)`; until the store milestone `since` is accepted and answered with a fresh Snapshot. Over leaving `since` out: the design puts it in milestone 1; over refusing it: reconnecting clients would be refused, not caught up.
4. `protocol_version` is the major version only, starting at `1` (`PROTOCOL_VERSION`); `Welcome(server_version)` carries `orbit.__version__`; a Hello with another major is refused with the new code `incompatible`, its message naming both versions, and nothing more is sent on that stream. Over `"major.minor"`: duplicates what tolerant decoding handles; over reusing `invalid`: clients couldn't tell a version problem from a malformed Hello.
5. The backend publishes nothing before Hello; any other command before it is refused with `invalid`; background work (the startup check, the usage monitor) starts once Welcome is sent; a second Hello is refused with `invalid`. Over buffering events from before Hello: older state events would roll back the Snapshot; over re-sending everything on a second Hello: it blurs the one-handshake rule.
6. After Welcome: `Catalog`, the latest `UsageChanged` if one was fetched, `Snapshot`, then one-off notices (the workspace note, "no verification steps configured", leftover work, resumed-session notes), then live events (settled with phase 1a for the order Welcome, Catalog, Snapshot). Over usage in the Snapshot: session-less state in a session's message; over usage in the Catalog: re-sent on every fetch.

## Catalog

7. `Catalog` lists `workspaces` (one `WorkspaceEntry`: id `"default"`, the repository's directory name as `label`, its `sessions` as `SessionSummary`s), `agents` (an `AgentEntry` per role the session has, custom roles included: `name`, `label`, `conversational`, true only for `planner`; built-ins labelled "Planner", "Coder", "Reviewer", "Super reviewer", "Configurator", custom roles by name), `triggers` (`TriggerEntry`: `super-review` "Super review" and `configure-checks` "Set up checks", both `manual`), and `lanes` (`work`, `planner`). Over built-in roles only: custom roles invisible; over no `lanes`: clients couldn't learn what `CancelTurn` accepts.
8. A new Catalog is sent whenever the sessions change (new, open, rename, delete) or the open session is renamed; `SessionsListed` stays only as the answer to `ListSessions` and for the startup picker. Over at connect only: the Catalog would go stale; over no sessions: departs from the design.

## Snapshot

9. `Snapshot` is one typed event: the session's `SessionSummary`, settings with model choices, each lane's busy flag, the tasks, the verification steps with their title, review status, the checks proposals waiting for a decision, and running agents (`RunningAgent`: id, role, task, open requests as `OpenRequest`). Over a burst of state events: the state spread out, and agents and proposals have no state event; over a list of events: clients would have to replay.
10. The Snapshot's own envelope `seq` is the point it is current to. Over a `current_to` field: it duplicates the envelope.
11. `SessionOpened` is retired: opening, starting or picking a session sends a Catalog then a Snapshot, in place of `TasksChanged` + `SettingsChanged` + `SessionOpened`. Over keeping it as a marker: redundant; over keeping the piecemeal events: two sources of the same truth.
12. With the startup picker: Welcome, Catalog, a Snapshot of the interim session, then `SessionsListed(reason="startup")`. Over no Snapshot until picked: a joining client would see nothing until the user acts.

## TUI

13. The TUI sends `Hello(client="orbit-tui <version>", protocol_version=1)` before any other command.
14. The TUI keeps a built-in key map from key to trigger name (ctrl+g → `super-review`, ctrl+e → `configure-checks`), takes each footer label from the Catalog, and hides a key whose trigger isn't in it; the command palette for keyless manual triggers waits for the triggers milestone. Over only storing the Catalog: unused; over building tabs from it: the tabs already work from events.
15. On a Snapshot the TUI replaces its task list, settings, busy flags, verification panel, review status and agents pane; opens a waiting proposal if none is shown and closes the shown one if the Snapshot has none; titles the conversation with the session's name, lets go of the task list and restarts its usage total from the session's spend. The transcript is untouched. Over clearing the transcript: a visible change before the store brings a transcript tail; over merging: stale state could survive.
16. No visible change, so README.md is unchanged.

## How it will be built

1. Write this record.
2. Open every stream with Hello and Welcome: `Hello`, `Welcome`, `PROTOCOL_VERSION` and `INCOMPATIBLE` in `orbit.protocol`; `AgentSession` publishes nothing and starts no background work before Hello and refuses as decided; the TUI sends Hello first; today's announcements follow Welcome.
3. Send a Snapshot after Welcome and after every switch: `Snapshot`, `RunningAgent`, `OpenRequest` in `orbit.protocol`; the backend builds it and sends it after Welcome (after the latest `UsageChanged`) and after every open, new or pick; the TUI replaces its state from it; the old announcements still arrive alongside.
4. Retire `SessionOpened` and the piecemeal announcements: a switch sends only the Snapshot; the constructor queues no state events; one-off notices follow the Snapshot; the startup picker's `SessionsListed` follows the Snapshot; the TUI drops its `SessionOpened` handling.
5. Send a Catalog on joining and when sessions change: `Catalog`, `WorkspaceEntry`, `AgentEntry`, `TriggerEntry` in `orbit.protocol`; sent after Welcome before usage and the Snapshot, and on new, open, rename and delete; the TUI labels ctrl+g and ctrl+e from it and hides a key whose trigger is missing.

## Out of scope

The transcript tail in the Snapshot, replay on `since` and `History` (store milestone); the JSON encoding, generated schema and `docs/protocol.md` (phase 1d); the command palette for triggers (triggers milestone).
