# Pause runs when Claude is down requirements

The decisions and plan for stopping a run when Claude's API is unavailable, instead of failing every task left in it, and starting it again by itself with a backoff, as runs already pause at the usage limit (`docs/00016-bugfix-fix-chains-and-usage-limits.md`).

## Goal

An API outage stops the run at the first task it hits; the run starts again by itself once Claude may be answering, at growing intervals; and the user can always see that the run is paused, why, and until when.

## Detecting it

1. An agent error means Claude's API is unavailable when its text matches Claude Code's API error forms, case-insensitively: `API Error: 500`, `502`, `503`, `504`, `529`, `overloaded_error`, `API Error (Connection error.)` or `Request timed out`, matched by `api_unavailable` in `src/orbit/agent/domain/usage_limits.py` beside `limit_hit`. Client errors (`400`, `401`, `403`, invalid request, authentication) don't match. Over any error before `get_task`: a broken setup would retry forever; over both: the same downside.

## The pause

2. A task ending in such an error stops the run as a usage limit does (a new `RunEnd`, `api_unavailable`); the task stays in error and the restarted run runs it first, afresh; a fix task carries on from the tree. Over returning it to pending: it would differ from the usage-limit rule; over a retry cap: a counter for a rare case.
3. The run starts again by itself after a backoff: 2, 5, 10, 20 minutes, then every 30 minutes, with no limit; ctrl+r starts it now, ctrl+s cancels the wait. Over a fixed 15 minutes: slow after a blip; over checking Claude's status page: an external dependency that can be wrong itself; over giving up after 6 hours: an overnight outage ends the night's work.
4. The backoff starts over once Claude answers again: as soon as any task's agent finishes its run without an API error, whatever the task's outcome. Over at run end only: mid-run recovery isn't seen; over never: slow to recover.
5. The wait works like the usage-limit wait: a review's work doesn't start the run early; switching sessions or closing orbit drops it; a wait ending while storage is halted starts nothing.
6. Only task runs pause. A review that hits an outage ends without suggestions, and the next landing reviews again; a planner turn that hits one fails, with the message in 8. Over pausing reviews too: a second retry path; over everything, planner included: resending a chat message by itself is surprising.

## Showing it

7. `SessionSummary` and `SessionStateChanged` gain `paused_until: datetime | None` and `paused_for: str | None` (`"usage_limit"` or `"api_unavailable"`), additive fields for both kinds of pause. The TUI shows "run paused: Claude's API isn't answering; trying again at 14:20" or "run paused: usage limit; starts again at 18:01" by the prompt and in the picker's state notes, until the run restarts or the wait is cancelled. Over Notices only: the reason scrolls away; over a new activity kind: a `Literal` change needing version 3.
8. Wording. Paused: "Claude's API isn't answering (`<first line of the error>`), so the run stopped. It starts again by itself at `<Wed 23 Oct 14:20 UTC>`; ctrl+r starts it now and ctrl+s cancels that." Restarting: "Starting the run again now Claude's API may be answering." A planner turn: "Claude's API isn't answering, so the planner couldn't reply; send your message again later." Over one generic message: an outage and a limit look the same.

## Docs

9. The schema is regenerated; `docs/protocol.md` gains `paused_until` and `paused_for`; README's section on the run's usage-limit pause covers API outages and the backoff.

## How it will be built

1. Write this record.
2. Pause the run while Claude's API is unavailable: `api_unavailable`, `outage_delay`, the `RunEnd` value `api_unavailable`, `WorkTurns` waiting with the backoff.
3. Say so when the planner can't reach Claude.
4. Show a paused run: `paused_until` and `paused_for`, the TUI's line and picker notes.

## Out of scope

Phase 4c's `waiting_for_coder` was added to `ActivityKind` at protocol version 2, contrary to `docs/00004-feature-wire-contract.md` decision 4; to be planned separately.
