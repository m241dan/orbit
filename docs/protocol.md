# The orbit protocol

A frontend talks to orbit's backend by sending commands and reading events. This document says
how that conversation goes. The models in `orbit.protocol` define every message, and the
generated JSON Schema in `schema/protocol.schema.json` says what each one looks like on the
wire. The decisions behind all this are recorded in `docs/00002-feature-envelope-and-outcomes.md`,
`docs/00003-feature-join-handshake.md`, `docs/00004-feature-wire-contract.md`, for the
transport, `docs/00009-feature-serve-and-connect.md` and, for attaching to sessions,
`docs/00010-feature-attach-and-lifecycle.md`.

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
- `workspace`: the workspace the command is for. There's one workspace for now, `"default"`
  (`DEFAULT_WORKSPACE`); a command naming another is refused with `unknown`.
- `traceparent` and `tracestate`: W3C trace context. They're optional, aren't checked, and
  aren't set yet.

```json
{"workspace":"default","traceparent":null,"tracestate":null,"command_id":"9c41e7","session":null,"payload":{"type":"remove_task","task_id":"t3"}}
```

An event travels in an `EventEnvelope`:

- `seq`: the event's place in the backend's stream, counted from 1. There's one counter per
  control plane, shared by every session, so one session's events can have gaps between them.
  With a store, it carries on from the highest `seq` the store's event log holds, and again
  when a saved session is opened, past any events another orbit logged since.
- `session`: the session the event belongs to. The event's type decides it: the types in
  `SESSIONLESS_EVENTS` (`UsageChanged`, `SessionsListed`, `Catalog` and `SessionStateChanged`)
  belong to no session, carry `null` and are never logged, and every other event carries the
  open session's id, but for the answers to a connection viewing another session (its
  `Snapshot`, `CommandAccepted` and `CommandRefused`), which carry that session's id and are
  never logged. Every event of a session is kept in the store's event log but for
  `CONNECTION_EVENTS` (`Welcome`, `Catalog`, `Snapshot` and `HistoryPage`), which answer one
  connection, and a connection's greeting (see [The handshake](#the-handshake)).
- `workspace`, `traceparent` and `tracestate`, as on commands.

```json
{"workspace":"default","traceparent":null,"tracestate":null,"seq":42,"session":"20261018-0801","payload":{"type":"command_refused","command_id":"9c41e7","code":"busy","message":"Task t3 is running."}}
```

## The handshake

Every stream opens with a `Hello` command:

```json
{"workspace":"default","traceparent":null,"tracestate":null,"command_id":"3f2a90","session":null,"payload":{"type":"hello","client":"orbit-tui 0.1.0","protocol_version":1,"since":null}}
```

`client` names the frontend, `protocol_version` is the major version it speaks
(`PROTOCOL_VERSION`, now `1`), and `since` is the last `seq` it saw, if it's reconnecting.

The backend publishes nothing but refusals before `Hello`. Every command, `Hello` included, is
first checked for its address: one naming another workspace is refused with `unknown`, and one
naming a session other than the one the connection views with `wrong_session`. Only then is a command other
than `Hello` sent before it refused with `invalid`. A second `Hello` is refused with `invalid`
too. A `Hello` naming another major
version is refused with `incompatible`, its message naming both versions, and nothing more is
sent on that stream.

Otherwise the backend answers `Hello` with `Welcome`, which carries the Hello's `command_id`
and the backend's `server_version`. `Welcome` is Hello's outcome: it takes the place of
`CommandAccepted`. What follows comes in this order:

1. `Catalog`: what exists, namely the workspaces with their sessions, the agents' roles, the
   triggers and the lanes, and `read_only`, true when another orbit owns the repository (see
   [Read-only orbits](#read-only-orbits)). Each session carries its state (see
   [Session state](#session-state)), the joining frontend counted among the open session's
   viewers.
2. The latest `UsageChanged`, if usage was fetched yet.
3. `Snapshot`: the open session as it stands, with its settings, busy lanes, tasks,
   verification steps, review status, checks proposals waiting for a decision, running
   agents and its `transcript`: the session's last 500 logged events, oldest first, in the
   envelopes they were sent in, their `seq`s included (none without a store), and, while the
   session is halted because a write to the store failed, its `storage_halt`: the explanation
   the halting `ErrorOccurred` carried (see `storage_failed` under [Codes](#codes)), and
   `read_only`, as the Catalog has it, with `ownership_offered`, true while an offer of the
   repository is open (see [Read-only orbits](#read-only-orbits)). A `Hello`
   whose `since` orbit can catch up from gets the open session's events after it here
   instead, in their original envelopes (see
   [Sequencing and reconnecting](#sequencing-and-reconnecting)).
4. One-off notices, such as the workspace note, a missing verification setup, leftover work or
   notes on a resumed session.
5. Live events, as things happen.

Steps 1 to 4, and an `ErrorOccurred` coded `internal` for any of them that fails, are the
connection's greeting: they reach the connection that said `Hello` alone, never the others
already connected, and aren't logged, so another frontend reconnecting isn't sent them again.

Each frontend shows its own session picker from the Catalog, kept current by
`SessionStateChanged`; orbit no longer sends `SessionsListed` with `reason="startup"`.

`Welcome` only says the Hello was accepted; it's the `Snapshot` that marks the frontend's state
as current. `Hello` attaches the connection to the workspace's open session (see
[Attaching](#attaching)).

Switching sessions works the way joining does: activating, starting or picking a session sends
a `Catalog` and then a `Snapshot` of the session now open (see [Activation](#activation)). A
rename or delete sends a `Catalog`.
A frontend replaces the conversation it shows with every Snapshot's transcript, so joining
shows where the session left off and a switch shows the session switched to.
`SessionsListed` only answers `ListSessions`, and after a rename or delete.

## Attaching

A connection views one session at a time: from its `Hello`, the workspace's open session,
whichever that is, until another session is activated in its place. `Attach(session)`
points it at the session `session` instead, and is answered with that session's `Snapshot`,
then `CommandAccepted`; one for a session there's none of is refused with `unknown`, and one for
a saved session that can't be read with `invalid`. From then on the connection gets the events
of the session it views, the events that belong to no session, and the answers to its own
commands; another session's events don't reach it. `Attach` with the open session's id views
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
frontend views it.`

A reconnecting `orbit.client.WebSocketSession` that was viewing a session other than the open
one sends its last `Attach` again after its `Hello`, so it views that session again.

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
to view, so its viewers view the session activated instead. `SessionStateChanged` follows for
both sessions (see [Session state](#session-state)).

`NewSession(name)` creates a session, `name`d so or by the time it started, and activates it in
the same way, under the same rule; the creating connection views it.

`OpenSession` is retired: it's refused with `invalid`, its message pointing to `Attach`, to view a
session, and `ActivateSession`, to make it the active one.

## Session state

Each session in a `Catalog` carries its state as it is when the Catalog is sent:

- `active`: whether it's the workspace's open session, the one that runs work.
- `state`: `running` while a task run, a review, a planner turn or the startup check is in
  progress, otherwise `idle`.
- `viewers`: how many welcomed connections view it: the open session's, and, for another
  session, those attached to it (see [Attaching](#attaching)).

Whenever one of them changes for a session, every welcomed connection gets
`SessionStateChanged(session, active, state, viewers)` with the new values: a task run, review,
planner turn or startup check starting or ending, a connection joining, closing or attaching to
another session, which sends one for the session it left and one for the session it views, and
another session activated, which sends one for the session left (inactive, idle, no viewers unless
connections attached to it) and one for the session activated. A connection joins once everything that follows its `Welcome` is sent, so the
change counting it comes after those. The event belongs to no session and isn't logged, and the
Catalog isn't re-sent for it.

## Read-only orbits

The first orbit to reach a repository, an `orbit serve` or an `orbit chat` running in-process,
owns it, and runs its work. Any other orbit on it is read-only: its `Catalog` and `Snapshot` have
`read_only` true, and a `Notice` after the Snapshot names the owner. A read-only orbit accepts
`RunTasks` and `FireTrigger` with `CommandAccepted` and queues them, in order, rather than running
them, after refusing, as an owning orbit would, those that couldn't run here (an unknown trigger,
input to one, or work without the agents, reviews or verification it needs), and starts no work by itself, such as reviews or the startup check; the planner, sessions,
drafts and settings work as usual. The queue is dropped when the orbit exits.

A read-only orbit tries the repository's lock every 5 seconds from its first `Welcome`. Once the
owner has gone, it takes the lock, holds it and publishes `OwnershipOffered()`, logged in the
open session, once until it's answered. `TakeOwnership(take)` answers it: with `take` true this
orbit owns the repository from then on, publishing `OwnershipTaken()` and a `Notice` ("This orbit
now owns the repository; queued work is running."), then running the work queued, in order, a
command refused now reported as an `ErrorOccurred` with the refusal's code; later `Catalog`s and
`Snapshot`s have `read_only` false. With `take` false it lets the lock go, so another orbit can
take it, publishing `OwnershipDeclined()`, stays read-only and offers the repository again the
next time it finds it free. While an offer is open, a `Snapshot` has `ownership_offered` true,
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
still waiting to be written count as logged. Then `Welcome`, the `Catalog` and
the latest usage, if any, are followed by the open session's events after `since`, oldest first
and in their original envelopes, `seq` and all, with no `Snapshot`; the one-off notices and the
rest of the handshake follow, then live events. Events no session owns, such as usage, aren't
logged, so aren't replayed.

Otherwise orbit sends a fresh Snapshot, as if `since` weren't given, and the frontend replaces
its state from it: when another session's events came after `since` (the user switched
sessions), when the open session has no event at or before `since` newer than every other
session's (the frontend's state is of another session, as when a new orbit opens a new or other
session), when `since` is older than what's kept, when it's ahead of the last event orbit
stamped, or when orbit keeps no event log.

## Deprecated

These messages are deprecated, listed in `DEPRECATED_MESSAGES` in `orbit.protocol`, and will be
removed at the next major `PROTOCOL_VERSION`; a test fails if that version is above 1 while any
of them is still in the protocol:

- `OpenSession`: refused with `invalid`; use `Attach` and `ActivateSession`.
- `ListSessions` and `SessionsListed`: still work; the `Catalog` and `SessionStateChanged` say
  the same.

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

`orbit serve` serves the protocol over a WebSocket on loopback, at the URL it prints and writes to
its run file (`ws://127.0.0.1:7433/` by default). Each WebSocket is a connection of its own, with
its own handshake and stream: it starts with `Hello` like any other, and what it's sent follows
the rules above, so every connection gets every event of the session it views and of none,
outcomes of other connections' commands included, but for the events answering one connection
(`Welcome`, `Catalog`, `Snapshot`, `HistoryPage`), which reach only it.

- **Authentication** is on the upgrade: the request must carry `Authorization: Bearer <token>`,
  the token in the token file of the server's config directory. Without it, or with another
  token, the server answers HTTP 401 and no WebSocket opens.
- **Frames**: each envelope is one text frame, in the wire format below. A frame that can't be
  decoded but has a `command_id` is answered with `CommandRefused` coded `unknown` for a command
  type the server doesn't know, else `invalid`; the connection stays open. One without a
  readable `command_id`, such as text that isn't JSON, or a binary frame, closes the WebSocket
  with code 1008 and the decoding error as the reason.
- **The message limit** is 64 MiB each way by default (`max_message_mb` in `[server]` of the
  server's `config.toml`). A larger frame from the frontend, or an event too large to send to
  it, closes the WebSocket with code 1009 and a reason saying how large it was.
- **Slow clients**: the server holds at most 10,000 events unsent to one connection. One more
  closes the WebSocket with code 1008 and the reason `too far behind`, and the session and its
  other connections carry on; the frontend reconnects with `Hello`'s `since` to catch up.
- **Shutting down**: as the server stops, after cancelling the work in progress, it closes every
  WebSocket with code 1001 and the reason `orbit serve is shutting down`.
- A stream that ends otherwise, as an incompatible `Hello`'s does, closes the WebSocket with code
  1000.

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
| `busy` | What the command acts on is running, so it can't be done now. |
| `invalid` | The command can't be done as given, such as a command before `Hello`, input to a trigger that takes none or `TakeOwnership` with no offer open. |
| `locked` | Another orbit holds what the command acts on, such as a session. |
| `wrong_session` | The command is for a session other than the one its connection views. |
| `not_active` | The connection views a session that isn't the workspace's open one, and the command would change or run something there (see [Read-only views](#read-only-views)). |
| `incompatible` | A `Hello` named a protocol version this orbit doesn't speak; nothing more is sent on that stream. |
| `storage_failed` | The session is halted because a write to the store failed; the message is the halt's explanation. Every command but `Hello` and `ResumeStorage` is refused so until storage is resumed; `ResumeStorage` is refused so while the writes still fail (see [Resuming storage](#resuming-storage)). |

Errors, in `ErrorOccurred`:

| Code | When |
| --- | --- |
| `agent_failed` | An agent's run or turn failed, or it exited without doing its task. |
| `timed_out` | An agent ran out of time. |
| `git_failed` | A git command or a worktree failed. |
| `save_failed` | Saving the session failed. |
| `storage_failed` | A write to the store (the event log, the session record or the review count) failed, so the session halted: its agents, the planner's turn and reviews are cancelled. The message explains which write failed, the database and its error, the likely cause and its fix, the unsaved events' `seq`s and the fallback file they were written to, and how to resume. |
| `checks_exhausted` | A check still failed after every fix attempt. |
| `internal` | Orbit itself failed unexpectedly. |
