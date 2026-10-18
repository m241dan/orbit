# Envelope and outcomes requirements

The decisions and plan for phase 1b of milestone 1 (generic protocol) of the control-plane design: every message travels in one envelope, every command gets exactly one outcome, and every refusal and error carries a stable code.

## Goal

A frontend can tell which events answer which command, order events by `seq`, and act on refusals and errors by code, through one envelope that later phases (Hello/Welcome, Snapshot, the wire encoding, telemetry, workspaces) add to without breaking it. No user-visible change beyond refusals clearing a stuck busy prompt.

## Envelope

1. Nested shape: envelope fields (`command_id` or `seq`, `workspace`, `session`, `traceparent`, `tracestate`), then `payload` holding the typed message with its `type`. Over flat: payload models would have to avoid the envelope's names forever; over copying `type` up: it would live in two places.
2. Two models, `CommandEnvelope` and `EventEnvelope`, on a shared base; `Session.send` takes a `CommandEnvelope` and `Session.events()` yields `EventEnvelope`s. Over one generic model: which field is required would be checked only at runtime; over both fields always optional: it allows meaningless envelopes.
3. `command_id` is required, a non-empty client-chosen string; the backend doesn't check uniqueness; the TUI uses `uuid4().hex`. Over optional: outcomes couldn't be matched; over checked uniqueness: ids would have to be remembered across reconnects.
4. `seq` is required on events and counted once per control plane, across sessions; gaps within one session's events are normal; the store milestone resumes it from the store. Over per session: numbers would repeat before the store exists; over per stream: meaningless after a reconnect.
5. `workspace` is required on both envelopes, defaulting to `DEFAULT_WORKSPACE = "default"` in `orbit.protocol`; a command naming another is refused with `unknown`. Over optional: the workspaces milestone would have to make it required; over ignoring other values: commands would act on the wrong workspace silently.
6. `session` is optional on commands, `None` meaning the open session; a command naming any other session is refused with `wrong_session`. Over required: it would change the startup flow that phase 1c rewrites; over ignored: the field would mean nothing.
7. `session` is optional on events and fixed by event type: `UsageChanged` and `SessionsListed` carry `None` (listed in `SESSIONLESS_EVENTS`), every other event the open session's id. Over always the open session: the control-plane-level stream would change these events' meaning; over deciding per event: no rule for clients.
8. `traceparent` and `tracestate` are optional strings, unchecked and unset until the telemetry milestone. Over checking the W3C format: the standard says ignore malformed values; over leaving them out: the design puts them in milestone 1.

## Command outcomes

9. Every command is answered by exactly one `CommandAccepted(command_id)` or `CommandRefused(command_id, code, message)` event.
10. Refusing code raises `Refused(code, message)`, an exception in the domain (`orbit.agent.domain.refusals`, so the task board's own refusals use it too); `AgentSession.send` catches it once and publishes `CommandRefused`, and otherwise publishes `CommandAccepted`. Code shared with background paths (`TurnSlot.start`, the keeper's lock checks) raises only when called for a command and publishes `ErrorOccurred` as before otherwise. Over handlers returning an outcome: every handler signature would change; over a context variable: the link would be hidden.
11. The outcome follows the events the command's handling published, such as `TasksChanged`.
12. Answers that are Notices today ("a configurator task is already queued or running", "nothing to reset") stay `CommandAccepted` followed by the same `Notice`. Over refusals: they would turn from 📌 notes into errors in the TUI; over a severity field: one more field for little gain.

## Coded errors

13. `code` is a `str`; the known codes are documented constants in `orbit.protocol`. Over a `Literal` or `StrEnum`: strict decoders would reject new codes.
14. The codes. Refusals: `not_configured`, `unknown`, `busy`, `invalid`, `locked`, `wrong_session`. Errors: `agent_failed`, `timed_out`, `git_failed`, `save_failed`, `checks_exhausted`, `internal`. Over one code per message: the list grows and duplicates meanings; over two codes: useless to programs.
15. `ErrorOccurred` keeps its name, gains a required `code`, and carries only errors with no command behind them. Over renaming: change for its own sake; over folding into `CommandRefused`: a crash isn't a refusal.

## Transport and TUI

16. `ThreadedSession` makes no events: a failed backend stream is raised from `events()`, and the TUI says the backend stopped. Over an optional `seq`: it weakens "every event carries `seq`"; over reusing the last `seq`: numbers stop being unique.
17. The TUI wraps every command in a `CommandEnvelope`, shows `CommandRefused` as an error line, ignores `CommandAccepted`, and clears the planner busy flag when its `SendMessage` is refused. Over showing only: the prompt stays stuck busy after a refusal; over a toast: it changes what users see.
18. No other user-visible change, so README.md is unchanged.

## How it will be built

1. Write this record.
2. Wrap commands and events in envelopes: `CommandEnvelope`, `EventEnvelope`, `DEFAULT_WORKSPACE` and `SESSIONLESS_EVENTS` in `orbit.protocol`; `Session` carries envelopes; `AgentSession` stamps `seq` and `session`; `ThreadedSession` forwards envelopes and raises a failed stream; the TUI wraps commands with a `uuid4().hex` `command_id`.
3. Answer every command with `CommandAccepted` or `CommandRefused`: those events and the refusal codes `NOT_CONFIGURED`, `UNKNOWN`, `BUSY`, `INVALID`, `LOCKED`, `WRONG_SESSION` in `orbit.protocol`; `Refused` in `orbit.agent.domain.refusals`, raised by the task board and by `AgentSession`'s own refusals and caught in `AgentSession.send`; commands naming another workspace or session refused; the TUI shows refusals and clears the planner busy flag on a refused `SendMessage`.
4. Turn the remaining command refusals into `Refused`: `PlannerSeat`, `SessionSwitcher`, `SessionKeeper` and `TurnSlot`, each raising only when called for a command.
5. Give `ErrorOccurred` a required `code`, with the error codes `AGENT_FAILED`, `TIMED_OUT`, `GIT_FAILED`, `SAVE_FAILED`, `CHECKS_EXHAUSTED`, `INTERNAL` in `orbit.protocol`, at every remaining site.

## Out of scope

The JSON wire encoding and tolerant decoding (phase 1d, with the generated schema and `docs/protocol.md`; over now: code with no user; over the server milestone: the compatibility rule untested until then). Hello/Welcome, Catalog, Snapshot and `since` (phase 1c).
