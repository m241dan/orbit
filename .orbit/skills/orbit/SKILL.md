---
name: orbit
description: Orbit's own rules — agents, tokens and held MCP calls; git, worktrees and the user's checkout; Orbit's modes; the protocol, sessions and schema; telemetry; and test stand-ins. Use when writing or reviewing Orbit's code.
---

# Orbit

Orbit runs coding agents through its own MCP server, in git worktrees, and verifies their work.
These rules come with that job.

## Agents

- Revoke an agent's token when its run ends, on every path that ends it.
- Answer every MCP call Orbit holds, on every path, cancellation and shutdown included.
- Treat an agent's output and the arguments of its tool calls as untrusted input, and parse them
  as they arrive.

## Git and the user's checkout

- Leave worktrees and branches in a good state: read git's exit status, abort a failed rebase in
  `finally`, and check every linked worktree before deleting anything.
- Keep the work when a merge fails.
- Touch the user's own checkout only when that is the point of the command.

## Modes

- Before starting work or accepting a command, check every mode that can forbid it: halted,
  read-only, paused for usage or an outage, closing, a disabled role. When the mode lifts,
  restart or re-arm what it held.
- Keep each mode, such as whether this orbit is read-only, in one place, and derive the rest from
  it.

## Protocol and sessions

- Give every command exactly one outcome, accepted or refused. Send each event to exactly the
  connections it concerns, and put in snapshots everything live events carry.
- Hand out ids and sequence numbers from a persisted high-water mark, so each is used once.
- After changing protocol models or the session record, regenerate the schema, and bump the
  format version with its fixture.
- An open set that a newer peer may extend (`TaskOrigin`, `PauseReason`) is a `str` alias, with
  its known values as constants in `orbit.protocol`.

## Telemetry

- Start long-lived background work with no current span, so it doesn't record on whatever
  started it.
- Record on a span the values actually used.
- Spans carry ids, titles, reasons and counts; transcript bodies stay out of them.

## Names in transition

Orbit is renaming its functions to verbs and its predicates to questions, package by package.
Write new and changed functions as verbs, and leave older noun names to the rename tasks. When a
task already changes a function's parameters, it also turns that function's bools into
enumerations and its unit-suffixed numbers into unit types.

## Tests

- Agents are played by a stand-in `claude` on `PATH`, driven by JSON scenarios
  (`tests/fake_claude.py`); git by throwaway repositories (`tests/git_repo.py`).
- Every test runs with isolated state and config directories, from the autouse fixtures in
  `tests/conftest.py`.
- TUI logic is tested with fake views, synchronously; widgets with Textual's pilot.
- Tests wait on conditions (`tests/waiting.py`, `tests/loop_quiet.py`), not fixed sleeps; a
  contract in `tests/test_contracts.py` fails on a sleep for a literal while unless its line
  says why with `# fixed wait: <why>`.

## Languages

Super reviews sample code files by their extension, from `CODE_EXTENSIONS` in
`orbit.agent.domain.review_sample`. When a plan brings in a language whose extension isn't on that
list, add the extension in the same plan.
