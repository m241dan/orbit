# orbit.execution requirements

The decisions and plan for the refactor `scratch/skills-design.md` settled as its decision 12:
moving Orbit's generic concurrency helpers into a top-level `orbit.execution`, the Execution code
type, which imports no orbit package.

## Goal

The helpers that know how work runs, and nothing of what the work is, live in one package that
the backend, the TUI and the CLI can all use: cancelling and shielding, running work in the
background, one turn at a time, and a session on its own thread. Behaviour doesn't change.

## Scope

1. The settled candidates move, and their importers switch over: `shielded`
   (`agent/external/shielding.py`), `cancelling` (`agent/application/outcomes.py`), `stop`,
   `waited_out`, `Background` and `TurnSlot` (`agent/application/lanes.py`), and
   `ThreadedSession` (`cli/threaded.py`); over sweeping every hand-written cancel-and-wait site in
   the same plan (some 21 files of async code at once).
2. The sweep of the 31 hand-written cancel-and-wait sites is planned on its own once this lands.

## Decided for you

3. Modules: `orbit.execution.cancelling` (`cancelling()`, `stop(task)`, `shielded(future)`, and
   `waited_out(work)` as `shielded(asyncio.ensure_future(work))`), `orbit.execution.background`
   (`Background`), `orbit.execution.turns` (`TurnSlot`) and `orbit.execution.threads`
   (`ThreadedSession`); over keeping both copies of the shielded wait.
4. The execution classes are generic cores taking callbacks: `Background` a crash callback and a
   task starter; `TurnSlot` busy and crash callbacks, raising its own `SlotBusy(refusal)`;
   `ThreadedSession` generic over small `Protocol`s of its own, which the protocol's `Session`
   and `SharedSession` satisfy. `orbit.agent.application.lanes` keeps `Background`, `TurnSlot`,
   `SessionGate` and `OPEN_GATE` under their names, as thin wrappers adding `crash_report`,
   `BusyChanged`, `Refused(BUSY)` and `no_span`, so their importers don't change; over
   `orbit.execution` importing `orbit.tracing` and `orbit.protocol` (it breaks the goal).
5. Two import contracts: "Execution imports no orbit package" (`orbit.execution` imports none of
   `orbit.agent`, `orbit.tui`, `orbit.protocol`, `orbit.cli`, `orbit.client`, `orbit.telemetry`,
   `orbit.profiling`, `orbit.tracing`) and "Neither the domain nor the protocol uses execution"
   (`orbit.agent.domain` and `orbit.protocol` don't import `orbit.execution`).
6. Tests move with the code: `orbit.execution` is tested directly, and the wrappers' tests check
   only what they add.
7. The `layers` skill's Execution row becomes "`orbit.execution`: cancelling and shielding,
   `Background`, `TurnSlot` and `ThreadedSession`. It imports no orbit package; a new generic
   helper goes here.", and its contracts list gains the two contracts.
8. This lands after the tasks of `docs/00037-feature-skill-delivery.md`, which touch the same
   lanes.

## How it will be built

1. Write this record.
2. Start `orbit.execution` with the cancellation helpers: `cancelling`, `stop`, `shielded`,
   `waited_out`; the two contracts; importers switched.
3. Move `Background` into `orbit.execution`, with the `lanes.Background` wrapper.
4. Move `TurnSlot` into `orbit.execution`, with `SlotBusy` and the `lanes.TurnSlot` wrapper.
5. Move `ThreadedSession` into `orbit.execution.threads`.
6. Update the `layers` skill's map.

## Deferred

- The sweep of hand-written cancel-and-wait and background-task code onto these helpers.
