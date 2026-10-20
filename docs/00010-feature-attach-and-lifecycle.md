# Attach and session lifecycle requirements

The decisions and plan for phase 3b of milestone 3 (control-plane server and remote client) of the control-plane design: sessions belong to the control plane, connections attach to them to view them, one session per workspace is the open one that runs work, and every frontend starts with a session picker.

## Goal

Looking at a session never changes what runs. Any frontend can browse any session; switching what the workspace runs is an explicit act, allowed only while nothing runs; and everyone can see which session is active, whether it's busy, and who's looking.

## Attaching

1. `Attach(session)` points a connection at a session and answers with its Snapshot. A connection views one session at a time and receives that session's events, session-less events and its own connection answers. `OpenSession` is retired: kept, deprecated, refused with `invalid` pointing to `Attach`/`ActivateSession`.
2. `Hello` attaches the connection to the workspace's open session, so `Welcome`, `Catalog`, `Snapshot` is unchanged; a reconnecting client re-sends `Attach` after `Hello` if it was viewing another session. Over attaching to nothing: breaks phase 1c's handshake rule and needs protocol version 2; over a `session` field on `Hello`: one more field for what `Attach` does.
3. A command for a session other than the connection's attached one is refused with `wrong_session`. Viewers arriving or leaving never change a session's running state; creating or attaching never starts work. A control plane serves one workspace (the repository it was started in) until milestone 4.

## Read-only views

4. Attaching to a session that isn't the open one gives a read-only view: its Snapshot from the saved record and the event log's tail, with `active: false`, `open_session` (id and name) and `viewers`. `History`, `RenameSession` and `DeleteSession` work; every other command is refused with the new code `not_active`, the message naming the open session and how to switch. Over allowing draft edits: two writers of one record; over view only: renaming would mean activating.

## Activation

5. `ActivateSession(session)` makes that session the workspace's open one; refused with `busy` unless the current one is idle. Viewers of the old one keep it as a read-only view; the activating connection now views a live session. `NewSession` creates a session and activates it under the same rule. Over activating on `Attach`: browsing would change what runs; over moving the other viewers: views change under people.
6. Idle means no task run, review or planner turn in progress; running means one is. The startup check doesn't block switching: it runs on the base commit, not in a session, and a failure adds its fix draft to whichever session is active then (see `docs/00013-bugfix-startup-check-in-session-views.md`).

## The picker

7. Every frontend starts with the session picker: "Resume <active>" first and preselected, then the other saved sessions, then "New session"; with none saved, only "New session"; `--new`/`--name` skip it; in-process `orbit chat` shows no "Resume". ctrl+p opens the same picker.
8. Picking an inactive session activates it directly when the active one is idle and no other frontend views it. Otherwise it attaches read-only and, if the active session is idle, offers "Make this the active session" or "Just look"; if it's running, the view names it. The same prompt opens from the view's mark (enter on it). Over always prompting: an extra step to resume; over in-process versus connected rules: a lone user on a server gets prompts for nothing; over activating whenever idle: picking would quietly change what runs.

## Session state

9. Catalog session entries gain `active`, `state` (`running` | `idle`) and `viewers`. A new session-less, unlogged event `SessionStateChanged(session, active, state, viewers)` carries their changes to every connection; the Catalog is re-sent only on joining and when the session list changes. The Catalog grows with saved sessions (about 0.5 KB each) and state flips often, so re-sending it whole on each change was rejected.
10. Deleting a session another frontend views is refused with `busy`, naming how many view it. Over moving its viewers: a view vanishes mid-read; over a dead view: a view of nothing.
11. Unloading idle sessions is deferred to milestone 4. Over unloading now: load and unload paths with little to save; over dropping it: milestone 4 may want it.

## Wording and protocol

12. The TUI shows two distinct marks by the prompt: "read-only: another orbit owns this repository" (phase 3a) and "viewing <name>, not the active session (enter to switch)"; each refusal's message says which applies. Over one mark: the reason scrolls away; over merging the concepts: they have different fixes.
13. `ListSessions` and `SessionsListed` stay, deprecated in `docs/protocol.md` and unused by the TUI. Over removing them now: a major bump for tidiness; over keeping them as they are: two ways to learn one thing.
14. Deprecated messages are listed in `DEPRECATED_MESSAGES` in `orbit.protocol` (`OpenSession`, `ListSessions`, `SessionsListed`) and in a **Deprecated** section of `docs/protocol.md`, to be removed at the next major `PROTOCOL_VERSION`; a test fails if `PROTOCOL_VERSION` is above 1 while any of them still exists.
15. The schema is regenerated; `docs/protocol.md` gains `Attach`, `ActivateSession`, `SessionStateChanged`, `not_active` and the deprecations; README's Sessions section describes the picker, attaching, read-only views and activation.

## How it will be built

1. Write this record.
2. Report session state and viewers: running/idle, viewers per attached connection, the Catalog's `active`/`state`/`viewers`, `SessionStateChanged`.
3. Attach connections to sessions: `Attach`; `Hello` attaching to the open session; events routed by attached session; `wrong_session`; read-only views with `Snapshot.active`, `open_session`, `viewers`; `NOT_ACTIVE`; deleting a viewed session refused; `orbit.client` re-sending `Attach` after reconnecting.
4. Activate sessions, and deprecate the old commands: `ActivateSession`; `NewSession` activating; `OpenSession` refused; `DEPRECATED_MESSAGES`, the Deprecated section and its test.
5. Open every frontend with the session picker: at startup and on ctrl+p, from the Catalog and `SessionStateChanged`; direct activation when idle and unviewed; the TUI stops using `ListSessions`.
6. Show and act on read-only session views: the viewing mark, the activation prompt, refusals naming which read-only applies.

## Deferred

Unloading idle sessions (milestone 4). Removing `OpenSession`, `ListSessions` and `SessionsListed` at the next major protocol version.
