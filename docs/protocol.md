# The orbit protocol

A frontend talks to orbit's backend by sending commands and reading events. This document says
how that conversation goes. The models in `orbit.protocol` define every message, and the
generated JSON Schema in `schema/protocol.schema.json` says what each one looks like on the
wire. The decisions behind all this are recorded in `docs/00002-feature-envelope-and-outcomes.md`,
`docs/00003-feature-join-handshake.md`, `docs/00004-feature-wire-contract.md`, for the
transport, `docs/00009-feature-serve-and-connect.md` and, for attaching to sessions,
`docs/00010-feature-attach-and-lifecycle.md` and, for workspaces and version 2,
`docs/00015-feature-many-workspaces.md`.

## Envelopes

Every message travels in an envelope. The envelope's own fields come first, then `payload`,
which holds the command or event itself, with its `type` naming which one it is.

A command travels in a `CommandEnvelope`:

- `command_id`: a non-empty string the sender chooses, such as a random hex id. The backend
  doesn't check that it's unique; it copies it into the command's outcome, so the sender can
  match the two.
- `session`: the session the command is for. It's optional: `null` means the session the
  connection views (see [Attaching](#attaching)), the open session unless it attached to
  another. A command naming any other session is refused with `wrong_session`.
- `workspace`: the id of the workspace the command is for, as the `Catalog` gives it. It's
  optional: `null` means the workspace the connection is attached to. A command naming a
  workspace that isn't served is refused with `unknown`, and one naming a workspace served but
  not the one attached with `wrong_session`, naming both, but for `Attach`, which moves the
  connection there (see [Attaching](#attaching)). The control plane's own commands,
  `AddWorkspace` and `RemoveWorkspace`, are for no workspace, and carry `null`.
- `traceparent` and `tracestate`: W3C trace context. They're optional, aren't checked, and
  aren't set yet.

```json
{"workspace":null,"traceparent":null,"tracestate":null,"command_id":"9c41e7","session":null,"payload":{"type":"remove_task","task_id":"t3"}}
```

An event travels in an `EventEnvelope`:

- `seq`: the event's place in the backend's stream, counted from 1. There's one counter per
  control plane, shared by every workspace and session, so one session's events can have gaps
  between them.
  With a store, it carries on past the highest `seq` the store has ever logged or reserved,
  and again when a saved session is opened, past any another orbit logged or reserved since.
  The store reserves them a block at a time, ahead of their being handed out, so a `seq` is
  never handed out twice, even once the events that had it are deleted, or when they were
  never logged, as the control plane's own aren't, and even while no workspace is served.
  `orbit serve`'s workspaces share one store, so each block is reserved in it once; should
  that fail, it's reserved with the next block, or as the server stops. A local orbit's
  store, while it's halted, reserves the block once it resumes.
- `session`: the session the event belongs to. The event's type decides it: the types in
  `SESSIONLESS_EVENTS` (`UsageChanged`, `Catalog`, `SessionStateChanged` and
  `WorkspaceCloning`)
  belong to no session, carry `null` and are never logged, and every other event carries the
  open session's id, but for the answers to a connection viewing another session (its
  `Snapshot`, `CommandAccepted` and `CommandRefused`), which carry that session's id and are
  never logged. Every event of a session is kept in the store's event log but for
  `CONNECTION_EVENTS` (`Welcome`, `Catalog`, `Snapshot` and `HistoryPage`), which answer one
  connection, and a connection's greeting (see [The handshake](#the-handshake)).
- `workspace`: the id of the workspace the event belongs to, but for the control plane's own
  events, the types in `CONTROL_PLANE_EVENTS` (`Welcome`, `Catalog`, `UsageChanged` and
  `WorkspaceCloning`), which carry `null`, as does the `ErrorOccurred` telling of a failed clone
  (see [Workspaces](#workspaces)), whose `session` is `null` too.
- `traceparent` and `tracestate`, as on commands.

```json
{"workspace":"4f1c2a9e","traceparent":null,"tracestate":null,"seq":42,"session":"20261018-0801","payload":{"type":"command_refused","command_id":"9c41e7","code":"busy","message":"Task t3 is running."}}
```

## The handshake

Every stream opens with a `Hello` command:

```json
{"workspace":null,"traceparent":null,"tracestate":null,"command_id":"3f2a90","session":null,"payload":{"type":"hello","client":"orbit-tui 0.1.0","protocol_version":2,"since":null}}
```

`client` names the frontend, `protocol_version` is the major version it speaks
(`PROTOCOL_VERSION`, now `2`), and `since` is the last `seq` it saw, if it's reconnecting.

The backend publishes nothing but refusals before `Hello`: a command other than `Hello` sent
before it is refused with `invalid`, and so is a second `Hello`. A `Hello` naming another major
version is refused with `incompatible`, its message naming both versions, and nothing more is
sent on that stream.

Otherwise the backend answers `Hello` with `Welcome`, which carries the Hello's `command_id`
and the backend's `server_version`. `Welcome` is Hello's outcome: it takes the place of
`CommandAccepted`. A `Catalog` follows it: what exists, namely the workspaces served, each with
its id, label, path, sessions and `read_only`, true when another orbit owns its repository (see
[Read-only orbits](#read-only-orbits)) and `managed`, true for a clone the server made from a
URL, whose directory purging it deletes, then the agents' roles, the triggers and the lanes. Each
session carries its state (see [Session state](#session-state)).

`Hello` attaches the connection to nothing, and nothing more reaches it until its first
`Attach(workspace, session)` (see [Attaching](#attaching)); every command but `Attach` and the
control plane's own, `AddWorkspace` and `RemoveWorkspace` (see [Workspaces](#workspaces)), sent
before then is refused with `invalid`. The first `Attach` is answered in this order:

1. `Snapshot`: the session as it stands, with its settings, busy lanes, tasks,
   verification steps, review status, checks proposals waiting for a decision, running
   agents and its `transcript`: the session's last 500 logged events, oldest first, in the
   envelopes they were sent in, their `seq`s included (none without a store), and, while the
   session is halted because a write to the store failed, its `storage_halt`: the explanation
   the halting `ErrorOccurred` carried (see `storage_failed` under [Codes](#codes)), and
   `read_only`, as the Catalog has it for the session's workspace, with `ownership_offered`, true while an offer of the
   repository is open (see [Read-only orbits](#read-only-orbits)). Attaching to the open
   session after a `Hello` whose `since` orbit can catch up from gets the open session's events
   after it here instead, in their original envelopes (see
   [Sequencing and reconnecting](#sequencing-and-reconnecting)).
2. The latest `UsageChanged`, if usage was fetched yet.
3. One-off notices, such as the workspace note, a missing verification setup, leftover work or
   notes on a resumed session.
4. The Attach's `CommandAccepted`.
5. Live events, as things happen.

`Welcome`, the `Catalog`, steps 1 to 3, and an `ErrorOccurred` coded `internal` for any of them
that fails, are the connection's greeting: they reach that connection alone, never the others
already connected, and aren't logged, so another frontend reconnecting isn't sent them again.
An `orbit serve` starts each workspace's background work, such as its startup check, as it
serves it, at start or once it's added, with no connection needed; a local `orbit chat` starts
it as the first connection attaches.

Each frontend shows its own session picker from the Catalog, kept current by
`SessionStateChanged`, and attaches to the workspace it opens on: connected to an `orbit serve`,
the TUI opens its workspace picker, highlighting the workspace of the repository it runs in, and
attaches to the session picked in the workspace chosen.

`Welcome` only says the Hello was accepted; it's the `Snapshot` that marks the frontend's state
as current.

Switching sessions works the way joining does: activating, starting or picking a session sends
a `Catalog` and then a `Snapshot` of the session now open (see [Activation](#activation)). A
rename or delete sends a `Catalog`, as does taking over a read-only workspace (see
[Read-only orbits](#read-only-orbits)). Each of these Catalogs, after a new session, an activation, a
rename or a delete, reaches every listening connection, not just the one whose command made the
change, so every frontend's picker follows new, renamed and deleted sessions; the connection that
asked gets it once, before its `Snapshot`. Orbit sends no Catalog when only a session's state
changes.
A frontend replaces the conversation it shows with every Snapshot's transcript, so joining
shows where the session left off and a switch shows the session switched to.
When the log can't be read, the transcript is empty and an `ErrorOccurred` coded `internal`
follows the Snapshot, saying the conversation's events couldn't be read and why.

## Attaching

A connection views one session at a time, of the workspace it's attached to. Its first
`Attach(workspace, session)` attaches it to the workspace `workspace` and views the session
`session` there, answered as [The handshake](#the-handshake) says; a later one views another
session of that workspace, answered with that session's `Snapshot`, then `CommandAccepted`. An
`Attach` for a workspace that isn't served, or a session there's none of, is refused with
`unknown`, and one for a saved session that can't be read with `invalid`. One for another
workspace served moves the connection there: it leaves the workspace it was attached to, whose
sessions' `viewers` count it no longer, and is answered as a first `Attach` is, with that
workspace's `Snapshot` and one-off notices; if the session is refused, the connection is
attached to no workspace, as before its first `Attach`. A connection viewing
the open session keeps viewing it, whichever that is, until another session is activated in its
place. From then on the connection gets the events
of the session it views, its workspace's events that belong to no session, the control plane's
own (the `Catalog`s and `UsageChanged`), and the answers to its own commands; another session's
events don't reach it, nor do another workspace's. `Attach` with the open session's id views
the open session again, live.

Every `Snapshot` says which session is the workspace's open one, the one that runs work:
`active` is true for that session, `open_session` gives its `id` and `name`, and `viewers`
counts the connections viewing the session snapshotted, the one asking included.

### Read-only views

Attaching to a saved session that isn't the open one gives a read-only view. Its `Snapshot`
comes from the session's saved record and its last logged events: `active` false, its saved
settings and tasks, its `transcript`, nothing busy or running, no steps, review or proposals,
and `open_session` naming the open session. Nothing runs in a viewed session, and it publishes
no events while it isn't the open one. On a view, `History`, `RenameSession`, `DeleteSession`,
`Attach`, `ActivateSession` and `NewSession` work as usual; every other command is refused with `not_active`, its message
naming the viewed session and the open one, such as `Session 'old work' isn't the active
session; 'today' is. Activate it to work in it.` The answers to a viewing connection's
commands reach it alone.

A session another connection views can't be deleted: `DeleteSession` is refused with `busy`,
its message saying how many connections view it, such as `Can't delete "old work": 1 other
frontend views it.` A connection can delete the session it views itself: once the session is
deleted, every connection still attached to it, the one deleting it or one that attached while
it was being deleted, views the open session again and gets the open session's live `Snapshot`,
to it alone, as `Attach` does; a `SessionStateChanged` reports the deleted session with no
viewers. The delete's `CommandAccepted` follows.

A reconnecting `orbit.client.WebSocketSession` sends its `Hello` again and, if it had attached,
its last `Attach`, for the workspace and session the last `Snapshot` it got showed, if it got
one since, so it views that session again.

## Workspaces

An `orbit serve` serves the workspaces added to it, and any connection, attached or not, can add
and remove them; each answer is a `Catalog` of the workspaces served now, to every listening
connection and the one that asked, then `CommandAccepted`. The other workspaces carry on as they
were.

`AddWorkspace(path)` serves the workspace at `path`, an absolute path on the server's host to a
directory: a repository's top level, which a path inside the repository stands for, or a plain
directory. It's registered, or found by its path with the sessions it had, opens its most
recently active session, takes its lock (or is read-only while another orbit holds it, see
[Read-only orbits](#read-only-orbits)) and starts its background work, such as its startup
check. Adding a workspace already served is accepted with nothing more done. It's refused with
`invalid` for a relative path or a file, or a workspace whose configuration is invalid or whose
repository git has no identity for commits in, with `unknown` for a path there's nothing at, and
with `internal` when orbit fails to make its lock, or fails partway through opening it in the
store, importing its old sessions or recovering its fallback files, which may have changed them.

```json
{"workspace":null,"traceparent":null,"tracestate":null,"command_id":"a71c03","session":null,"payload":{"type":"add_workspace","path":"/home/me/src/orbit","url":null}}
```

`AddWorkspace(url)` serves a clone of the repository at `url`, which the server makes: an
`https://`, `ssh://` or `file://` URL, a scp-style `user@host:path`, or an absolute path on the
server's host (cloned, not served in place). An `AddWorkspace` is given `path` or `url`, never
both; both, neither, or a `url` of any other kind, such as `ftp://x`, is refused with `invalid`.
It's accepted at once, and git clones the repository in the background, so other commands carry
on meanwhile; a second `AddWorkspace` with the same `url` while it's being cloned is refused with
`busy`. The clone goes to the server's clone directory (`clone_dir` in `[server]`, by default
`workspaces` in its state directory), in a directory named after the URL's last part without
`.git` (`proj` for `https://github.com/o/proj.git`), or `proj-2`, `proj-3` and so on when that's
taken. Git runs as the server's account, with its credential helpers and SSH agent, and never
prompts: frontends never send credentials.

```json
{"workspace":null,"traceparent":null,"tracestate":null,"command_id":"b52d10","session":null,"payload":{"type":"add_workspace","path":null,"url":"git@github.com:o/proj.git"}}
```

As git clones, `WorkspaceCloning(url, phase, percent)` events report its progress: the `phase`
git is in, such as `Receiving objects`, and how far through it it is, `percent`, when git says
(`null` when it doesn't). Once the clone is made, it's served as one added by its path is,
marked in the registry as managed, a clone orbit made, and a `Catalog` listing it follows. If
the clone fails, an `ErrorOccurred` coded `git_failed` gives git's last lines, such as
`Couldn't clone git@github.com:o/proj.git: ERROR: Repository not found.`, with, when they look
like git lacked credentials, the hint `The server's account needs access to this repository: an
SSH key or a git credential helper.`, and what was cloned is removed; if the clone can't be
served, the `ErrorOccurred` carries the code adding its path would be refused with. The
progress, the failure and that `Catalog` reach every connection that's listening and every one
welcomed but attached to no workspace, as whichever asked may not be attached yet; they carry
no workspace and no session, and aren't logged. A clone still under way when the server stops
is stopped, and what it cloned removed.

```json
{"workspace":null,"traceparent":null,"tracestate":null,"seq":57,"session":null,"payload":{"type":"workspace_cloning","url":"git@github.com:o/proj.git","phase":"Receiving objects","percent":42}}
```

`RemoveWorkspace(workspace)` stops serving the workspace whose id is `workspace`: its host
closes, its lock is let go and its sessions are kept, so adding it again finds them. The
connection removing it, if it was attached there, is attached to no workspace again, and its
next `Attach` is answered as a first one is, with a snapshot, as the `since` its `Hello` gave
was of the workspace removed. It's refused with `unknown` for a workspace that isn't served, and
with `busy` while the workspace runs work, its message naming what runs, such as
`Can't remove /home/me/src/orbit: tasks are running.`, or while another connection views one of
its sessions, its message naming them. It's refused with `internal` if orbit fails to stop flagging it as
served in the store, before anything else is done, so it's still served, and served again by
a later `orbit serve`: `Can't remove /home/me/src/orbit, which is still served: …`.

`RemoveWorkspace(workspace, purge=true)` (`purge` defaults to `false`) removes the workspace in
the same way, under the same refusals, then deletes everything orbit keeps of it: its sessions,
their events and logs, its review count, its session locks, its fallback files and its entry in
the registry, so adding it again starts afresh, and, for a clone orbit made (see `url` above), the
clone's directory. A directory orbit didn't clone into is never deleted. Before anything is
removed, a clone is checked for work that exists nowhere else: uncommitted changes, stashes and
commits on any branch that none of its remotes has. If it holds any, the purge is refused with
`invalid`, listing it, and nothing is removed: `Can't purge /srv/orbit/workspaces/proj: deleting
its clone would lose 1 commit on main not on its remote.` Push the work, or drop it, and purge
again. If deleting fails once the workspace is removed, it's refused with `internal`: `Removed
/home/me/src/orbit, but couldn't purge it: …`; the workspace is no longer served.

```json
{"workspace":null,"traceparent":null,"tracestate":null,"command_id":"c83e52","session":null,"payload":{"type":"remove_workspace","workspace":"4f1c2a9e","purge":true}}
```

A local `orbit chat` serves its own repository alone, and refuses both with `invalid`: `A local
orbit works on one repository; run orbit serve to work on several.`

## Activation

`ActivateSession(session)` makes the saved session `session` the workspace's open one, the one
that runs work: its tasks, review state, planner conversation and settings replace the open
session's, what was running when it was saved is marked interrupted, and nothing runs until
asked. It's refused with `busy` unless the open session is idle, its message naming what runs,
such as `Can't switch: tasks are running. Press ctrl+s to stop them.`; with `unknown`,
`invalid` or `locked` for a session there's none of, one that can't be read, or one another
orbit has open. Activating the open session attaches to it, as `Attach` does.

The activating connection then views the session activated, live, and gets its `Catalog` and
`Snapshot`, then `CommandAccepted`. Every other connection keeps viewing what it viewed: those
that viewed the session left keep it as a read-only view and get a fresh `Snapshot` of it, with
`active` false and `open_session` naming the session activated, and those attached to the
session activated get its `Snapshot`, now live. A session left that was never saved has nothing
to view, so its viewers view the session activated instead, live, and get its `Catalog`, which
no longer lists the session left, and its `Snapshot`. `SessionStateChanged` follows for
both sessions (see [Session state](#session-state)).

`NewSession(name)` creates a session, `name`d so or by the time it started, and activates it in
the same way, under the same rule; the creating connection views it.

## Session state

Each session in a `Catalog` carries its state as it is when the Catalog is sent:

- `active`: whether it's the workspace's open session, the one that runs work.
- `state`: `running` while a task run, a review or a planner turn is in progress, otherwise
  `idle`. The startup check runs on the base commit, not in a session, so it leaves the state
  `idle`.
- `viewers`: how many welcomed connections view it: the open session's, and, for another
  session, those attached to it (see [Attaching](#attaching)).
- `activity`: what's in progress in the open session, in this order, each an
  `Activity(kind, task_id)`: a `task_run` of task `task_id`, a `review` of task `task_id`, the
  task whose work it reviews, a `planner` turn and the `startup_check`, whose `task_id` is null.
  A review of several tasks' work, such as a batch's, is one `review` per task, in task order;
  a super review reviews no particular task, so it's one `review` whose `task_id` is null. It's
  empty for any other session. The startup check shows here though it leaves the state `idle`.

Whenever one of them changes for a session, every welcomed connection gets
`SessionStateChanged(session, active, state, viewers, activity)` with the new values: a task run,
review, planner turn or the startup check starting or ending, a run moving on to its next task, a
connection joining, closing or attaching to
another session, which sends one for the session it left and one for the session it views, and
another session activated, which sends one for the session left (inactive, idle, no viewers unless
connections attached to it) and one for the session activated. A connection joins once everything that follows its `Welcome` is sent, so the
change counting it comes after those. The event belongs to no session and isn't logged, and the
Catalog isn't re-sent for it.

## Read-only orbits

The first orbit to reach a repository, an `orbit serve` or an `orbit chat` running in-process,
owns it, and runs its work. Any other orbit on it is read-only: the workspace's entry in its
`Catalog`, and its `Snapshot`, have `read_only` true, and a `Notice` after the Snapshot names the owner. A read-only orbit accepts
`RunTasks` and `FireTrigger` with `CommandAccepted` and queues them, in order, rather than running
them, after refusing, as an owning orbit would, those that couldn't run here (an unknown trigger,
input to one, or work without the agents, reviews or verification it needs), and starts no work by itself, such as reviews or the startup check; the planner, sessions,
drafts and settings work as usual. The queue is dropped when the orbit exits.

A read-only orbit tries the repository's lock every 5 seconds from its first `Welcome`. Once the
owner has gone, it takes the lock, holds it and publishes `OwnershipOffered()`, logged in the
open session, once until it's answered. `TakeOwnership(take)` answers it: with `take` true this
orbit owns the repository from then on, publishing `OwnershipTaken()` and a `Notice` ("This orbit
now owns the repository; queued work is running."), then a `Catalog` to every listening
connection, whatever workspace it's attached to, whose entry for the workspace has `read_only`
false, then running the work queued, in order, a command refused now reported as an
`ErrorOccurred` with the refusal's code; the workspace's entry in later `Catalog`s, and later
`Snapshot`s, have `read_only` false too. Declining sends no `Catalog`, as nothing in it changes. With `take` false it lets the lock go, so another orbit can
take it, publishing `OwnershipDeclined()`, stays read-only and offers the repository again once
it has found another orbit holding the lock since and then finds it free, or, if it stays free,
after 5 minutes. While an offer is open, a `Snapshot` has `ownership_offered` true,
so a frontend that connects after `OwnershipOffered` went out can still answer it; once
`OwnershipTaken` or `OwnershipDeclined` settles it, from whichever frontend answered, the others
close the offer they show.
`TakeOwnership` is refused with `invalid` when no offer is open, as it always is for an owner.

## The user's messages and decisions

When the backend accepts a `SendMessage`, it publishes `MessageSent(command_id, agent, text)`
before the agent's turn on it, and when it records a `DecideProposal`, it publishes
`ProposalDecided(command_id, agent_id, task_id, action, feedback)`, `agent_id` naming the agent
that proposed the checks. Both come before the command's `CommandAccepted`, and both are logged,
so a Snapshot's transcript and `HistoryPage`s hold what the user said along with what was
answered. A refused message or decision publishes neither.

Each carries the `command_id` of the command it accepts, so a frontend can tell its own from
another frontend's. The TUI shows its user's prompt and decision as soon as they're sent, and
skips the echo of a command it sent itself; it shows every other `MessageSent` and
`ProposalDecided`, live or in a transcript, as it shows its own: the message as a prompt under
the agent's tab, the decision as orbit's note to the agent that proposed.

## Paging through history

The Snapshot's transcript holds a session's last 500 events; `History` reads further back.
`History(session, before_seq, limit)` asks for the session's logged events before `before_seq`,
such as the oldest `seq` the frontend shows: the latest `limit` of them, 200 unless given, and
from 1 to 500. The backend answers with a `HistoryPage` and then `CommandAccepted`. The page
holds the `session`, its `events`, oldest first, in the envelopes they were sent in, and `more`,
whether older events remain, to ask for with the oldest `seq` of this page; `trimmed`, on the
page with no more before it, says whether older events were removed from the log because the
session's log grew past `session_log_mb`, and is false on every other page. A `History` for a session that isn't this workspace's is refused
with `unknown`, and one whose log can't be read, such as while the database stays locked, is
refused with `internal`, naming the database and the error, so the frontend can ask again;
without a store, the page is empty and `more` is false. The TUI asks for the
page before the oldest event it shows when the cursor moves up past the top of the
conversation, one request at a time.

## Resuming storage

While the session is halted because a write to the store failed (see `storage_failed` under
[Codes](#codes)), `ResumeStorage()` retries the writes that failed: the session record as it is
now, the review count and appending to the event log. When they work, the events parked in the
fallback file and those still queued are appended in `seq` order, skipping any the log holds
already, the fallback file is deleted, the halt is lifted (commands are accepted again, and
later Snapshots' `storage_halt` is null) and the command is answered with `CommandAccepted`. For
each run of events that reached neither the log nor a fallback file, the backend publishes and
logs a `LogGap(first_seq, last_seq)` before that outcome, naming the first and last `seq` lost;
the Snapshot's transcript and `HistoryPage`s hold it in its place, like any logged event. While
a write still fails, `ResumeStorage` is refused with `storage_failed`, its message the
explanation made afresh for the failure now, and the session stays halted. While the session
isn't halted, `ResumeStorage` is accepted and does nothing.

## Command outcomes

The backend answers every command with exactly one outcome, `CommandAccepted` or
`CommandRefused`, carrying the command's `command_id` (`Hello` is answered with `Welcome` in
place of `CommandAccepted`). The outcome comes after the events the command's handling
published, such as a `TasksChanged`, so when a frontend sees it, it has seen what the command
did. `Hello` is the exception: `Welcome` comes first, and the handshake's events follow it, so a
frontend that has seen `Welcome` hasn't yet seen the `Snapshot`. If orbit fails to gather the
catalog, `Hello` is refused as `internal` with no `Welcome`; a step that fails after `Welcome` is
reported as an `ErrorOccurred` with code `internal`, not as a second outcome, and the handshake's
remaining events still follow it.

`CommandRefused` says why in `code`, one of the refusal codes below or `internal` when orbit failed
handling the command, and `message`, for people.
A refused command changed nothing, except one refused as `internal`: orbit failed partway
through handling it, so it may have changed something.

`ErrorOccurred` reports an error no command caused, such as an agent failing during a task
run, with `code`, one of the error codes below or a refusal code where it fits, and `message`.

## Sequencing and reconnecting

Events arrive in `seq` order. The Snapshot's own envelope `seq` is the point it's current to:
it includes the effect of every event before it, and every later event applies on top of it.

A reconnecting frontend sends the last `seq` it saw as `Hello`'s `since`. Orbit replays what it
missed when the open session's logged events are all it missed: the last logged event at or
before `since` belongs to the open session (it has one there, newer than every other session's),
every logged event with a `seq` after `since` belongs to the open session, and `since` is no older
than that session's oldest kept event (`since` is at least that event's `seq` minus one). Events
still waiting to be written count as logged. Then, once the connection attaches to the open
session, its events after `since` are sent, oldest first and in their original envelopes, `seq`
and all, with no `Snapshot`; the latest usage, the one-off notices and the rest of the greeting
follow, then live events. Events no session owns, such as usage, aren't
logged, so aren't replayed.

Otherwise, or when the connection attaches to another session, orbit sends a fresh Snapshot, as
if `since` weren't given, and the frontend replaces
its state from it: when another session's events came after `since` (the user switched
sessions), when the open session has no event at or before `since` newer than every other
session's (the frontend's state is of another session, as when a new orbit opens a new or other
session), when `since` is older than what's kept, when it's ahead of the last event orbit
stamped, or when orbit keeps no event log. A session's events that were deleted, as when an
unsaved session is left or a session is deleted, count as another session's: a frontend that
last saw them gets a fresh Snapshot.

## Deprecated

Deprecated messages are listed in `DEPRECATED_MESSAGES` in `orbit.protocol`, to be removed at
the next major `PROTOCOL_VERSION`. There are none: version 2 removed `OpenSession`,
`ListSessions` and `SessionsListed`, which version 1 deprecated, along with the `"default"`
workspace id; `Attach` and `ActivateSession` replace `OpenSession`, and the `Catalog` and
`SessionStateChanged` say what `SessionsListed` did.

## Compatibility

A peer may be older or newer than the one it talks to, so changes to the protocol are additive:

- Decoding from the wire is tolerant. Fields a decoder doesn't know are ignored, and event types
  it doesn't know are skipped. Messages built in-process are strict, so a typo in a field name
  still fails there.
- Something is added only as a new field, with a default where older peers won't send it, or
  as a new message type.
- A new value in an existing `Literal` field, removing a field or a message, or changing what
  one means breaks older peers, and needs a new major `PROTOCOL_VERSION`.

Codes are plain strings rather than `Literal`s, so a new code doesn't break decoding; a
frontend should treat a code it doesn't know as a generic refusal or error.

## Wire format

On the wire, each envelope is JSON text, one message per frame. `encode` writes every field,
nulls and defaults included, as compact JSON, so a reader needn't know any defaults.
`decode_command` and `decode_event` read it back. Both raise `ProtocolError` for text that
isn't JSON, isn't an object, or holds an invalid message; its `command_id` is the envelope's
when it could be read. `decode_event` returns `None` for an event type it doesn't know, to be
skipped, while `decode_command` raises a `ProtocolError` marked `unknown` for a command type it
doesn't know.

## Transport

`orbit serve` serves the protocol over a WebSocket, at the URL it prints and writes to its run
file (`ws://127.0.0.1:7433/` by default). Each WebSocket is a connection of its own, with
its own handshake and stream: it starts with `Hello` like any other, and what it's sent follows
the rules above, so every connection gets every event of the session it views and of none,
outcomes of other connections' commands included, but for the events answering one connection
(`Welcome`, `Catalog`, `Snapshot`, `HistoryPage`), which reach only it.

- **TLS**: with `tls = "off"` (the default) the server speaks plaintext `ws://` and listens on
  loopback alone. With `tls = "tls"` (`--tls tls`) it terminates TLS itself with the server
  certificate and key in `[server]` (`cert`, `key`), at a `wss://` URL, on any address; clients
  verify the certificate and its hostname. With `tls = "mtls"` the handshake also asks for a
  client certificate, and drops the connection if it shows one the CA in `client_ca` didn't
  sign; one that shows none gets through the handshake, for the health checks, and is answered
  HTTP 401 on the upgrade and every other path, with `WWW-Authenticate: ClientCertificate`.
  TLS changes nothing above the WebSocket.
- **Authentication** is on the upgrade: the request must carry `Authorization: Bearer <token>`,
  the token in the token file of the server's config directory. Without it, or with another
  token, the server answers HTTP 401 and no WebSocket opens. Under mutual TLS the client
  certificate is the identity instead and any token is ignored; one whose SHA-256 fingerprint is
  in `[server] revoked` is answered HTTP 403, naming the fingerprint.
- **Frames**: each envelope is one text frame, in the wire format below. A frame that can't be
  decoded but has a `command_id` is answered with `CommandRefused` coded `unknown` for a command
  type the server doesn't know, else `invalid`; the connection stays open. One without a
  readable `command_id`, such as text that isn't JSON, or a binary frame, closes the WebSocket
  with code 1008 and the decoding error as the reason.
- **The message limit** is the server's: 64 MiB each way by default (`max_message_mb` in
  `[server]` of the server's `config.toml`), enforced by the server both on the frames it
  accepts and on the events it sends. A larger frame from the frontend, or an event too large to
  send to it, closes the WebSocket with code 1009 and a reason saying how large it was. The
  frontend sets no limit of its own, taking any event the server sends, so a server with a
  raised limit is followed. A frontend doesn't reconnect after a 1009, as replaying what it
  missed would send the same event again.
- **Slow clients**: the server holds at most 10,000 events unsent to one connection. One more
  closes the WebSocket with code 1008 and the reason `too far behind`, and the session and its
  other connections carry on; the frontend reconnects with `Hello`'s `since` to catch up.
- **Shutting down**: as the server stops, after cancelling the work in progress, it closes every
  WebSocket with code 1001 and the reason `orbit serve is shutting down`. An upgrade that
  arrives as it stops is answered HTTP 503, with that reason as the body, and no WebSocket opens.
- A stream that ends otherwise, as an incompatible `Hello`'s does, closes the WebSocket with code
  1000. A frontend doesn't reconnect after it, as the same `Hello` would be refused again.
- **Health checks**: `GET /healthz` and `GET /readyz` answer on the same port without the token,
  in `application/health+json`, the IETF health-check draft's minimal form; the status code is
  the contract. `/healthz` answers 200 `{"status": "pass"}` while the server runs. `/readyz`
  answers 200 `{"status": "pass"}` once the store is open, the workspaces loaded and frontends
  accepted, with storage working; 200 `{"status": "warn", "output": "read_only"}` while another
  orbit owns the repository of any workspace served; and otherwise 503 `{"status": "fail", "output": <reason>}`, the
  reason `starting`, `shutting_down` or `storage_halted`. Neither says more: no version, paths or
  checks. Under mutual TLS neither needs a client certificate.

Work doesn't depend on any frontend: queued work goes on to the end with none connected.

## The schema

`schema/protocol.schema.json` is a JSON Schema (draft 2020-12) generated from the models. Its
top level accepts a command envelope or an event envelope, every message is under `$defs`, and
`x-protocol-version` records the protocol version.

It describes the wire: it keeps required fields, `Literal` value lists and the `type`
discriminator, and every object allows extra properties, so a message from a newer peer still
validates. It says what a message looks like, not how the conversation goes: the order of
messages, what each code means and which session an event carries are described here.

A test fails when the file differs from the models. To regenerate it after changing them, run:

```
uv run python -m orbit.protocol.schema
```

## Codes

The codes are gathered in `orbit.protocol` as `REFUSAL_CODES` and `ERROR_CODES`.

Refusals, in `CommandRefused`:

| Code | When |
| --- | --- |
| `not_configured` | What the command needs isn't set up, such as agents or verification. |
| `unknown` | The command names something there's none of, such as a task, a lane or a workspace. |
| `busy` | What the command acts on is running, so it can't be done now, such as a URL `AddWorkspace` is cloning already. |
| `invalid` | The command can't be done as given, such as a command before `Hello`, a command for a workspace before the connection is attached, a relative path to `AddWorkspace`, or both or neither of its `path` and `url`, or a `url` that's neither a URL it clones nor an absolute path, adding or removing a workspace on a local orbit, input to a trigger that takes none or `TakeOwnership` with no offer open. |
| `locked` | Another orbit holds what the command acts on, such as a session. |
| `wrong_session` | The command is for a session other than the one its connection views, or a workspace other than the one it's attached to. |
| `not_active` | The connection views a session that isn't the workspace's open one, and the command would change or run something there (see [Read-only views](#read-only-views)). |
| `incompatible` | A `Hello` named a protocol version this orbit doesn't speak; nothing more is sent on that stream. |
| `storage_failed` | The session is halted because a write to the store failed; the message is the halt's explanation. Every command but `Hello` and `ResumeStorage` is refused so until storage is resumed; `ResumeStorage` is refused so while the writes still fail (see [Resuming storage](#resuming-storage)). |

Errors, in `ErrorOccurred`:

| Code | When |
| --- | --- |
| `agent_failed` | An agent's run or turn failed, as when orbit's MCP server couldn't start or the checks it proposed couldn't be saved, or it exited without doing its task. |
| `timed_out` | An agent ran out of time. |
| `git_failed` | A git command or a worktree failed, or cloning a repository `AddWorkspace` named by its URL did. |
| `save_failed` | Saving the session failed. |
| `storage_failed` | A write to the store (the event log, the session record or the review count) failed, so the session halted: its agents, the planner's turn and reviews are cancelled. The message explains which write failed, the database and its error, the likely cause and its fix, the unsaved events' `seq`s and the fallback file they were written to, and how to resume. |
| `checks_exhausted` | A check still failed after every fix attempt. |
| `internal` | Orbit itself failed unexpectedly. |
