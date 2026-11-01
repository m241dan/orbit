# Cancel sweep requirements

The decisions and plan for moving Orbit's hand-written cancel-and-wait code onto the helpers in
`orbit.execution`, the sweep `docs/00038-refactor-orbit-execution.md` deferred.

## Goal

Code that cancels a task and waits for it does so through `orbit.execution`'s helpers, written
once, so a cancellation meant for the caller is never swallowed and each pattern has one
implementation.

## Scope

1. Eight sites in three patterns move: cancel then await suppressing `CancelledError`
   (`client/session.py`, `cli/status.py`, `tui/event_stream.py`); cancel several then gather with
   `return_exceptions=True` (`agent/application/agent_run.py`, `agent/application/clones.py`,
   `agent/external/embedded_server.py`); cancel then `asyncio.wait` and read the outcome
   (`agent/application/desk.py`, `agent/application/proposal_trial.py`).
2. The three suppressing sites move onto `stop`, so a cancellation meant for the caller is no
   longer swallowed; each is pinned by a test that a cancelled caller stays cancelled; over
   keeping today's behaviour (the bug stays).
3. Every other site keeps its behaviour exactly.

## Out of scope

- Methods merely named `cancel` (`seat.cancel()`, `editor.cancel()`, `runner.cancel()`), which
  cancel no task directly.
- Tasks cancelled and left, such as the CLI's stderr reader in `cli_turns.py` and the
  consultation timer in `consult_approvals.py`: the conventions ask that every task be awaited or
  cancelled, and these are cancelled.
- The lanes' own agent tasks (`work_lane.py`, `lane_runner.py`), awaited by the code running them.
- The log writer and seq mark, which drain to completion rather than cancel.

## Decided for you

4. Two helpers join `orbit.execution.cancelling`: `stop_all(tasks)` cancels each, waits for all
   and raises none of their outcomes, a cancellation meant for the caller going on; and
   `cancel_and_wait(task)` cancels it and waits until it's done, raising nothing of its own, so
   the caller reads its outcome after; over leaving those sites hand-written.
5. `agent_run.py`, `clones.py` and `embedded_server.py` move onto `stop_all`; `desk.py` and
   `proposal_trial.py` onto `cancel_and_wait`; `client/session.py`, `cli/status.py` and
   `tui/event_stream.py` onto `stop`.
6. No import contract changes: the client, the TUI and the CLI may already import
   `orbit.execution`.

## How it will be built

1. Write this record.
2. Add `stop_all` and `cancel_and_wait` to `orbit.execution`.
3. Move the backend's group and outcome-reading cancels onto the helpers.
4. Stop swallowing callers' cancellations in the client, the CLI and the TUI.
