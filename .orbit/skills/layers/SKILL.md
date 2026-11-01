---
name: layers
description: Orbit's map of code types — which packages hold Domain, Application, External, Execution and Entry code, where ports, adapters, wire models and TUI code go, and the import contracts that keep them apart. Use when placing or reviewing code in Orbit.
---

# Orbit's code types

| Type | Where it lives |
|---|---|
| Domain | `orbit.agent.domain`; the plain functions in `orbit.tui.view` (presentation rules); `orbit.protocol` (wire models) |
| Application | `orbit.agent.application`, with its ports and their exceptions in `application/ports.py`; the controllers in `orbit.tui.view` |
| External | `orbit.agent.external`, one adapter per outside system; `orbit.telemetry`; `orbit.client`; the Textual widgets in `orbit.tui` |
| Execution | `orbit.execution`: cancelling and shielding (`orbit.execution.cancelling`), `Background` (`orbit.execution.background`), `TurnSlot` (`orbit.execution.turns`) and `ThreadedSession` (`orbit.execution.threads`). It imports no orbit package; a new generic helper goes here. |
| Entry | `orbit.cli` and `orbit.__main__` |
| Ports and tools | `orbit.tracing` (the tracing port, standard library only) and `orbit.profiling` (wired in by the CLI) |

## What the import contracts hold

`pyproject.toml`'s import contracts, checked by `lint-imports`, keep this shape:

- The backend (`orbit.agent`) and the frontend (`orbit.tui`) are independent. They meet only in
  `orbit.cli`, through `orbit.protocol`, which imports neither.
- The backend is layered external > application > domain, and uses no UI library (textual,
  rich, typer).
- `orbit.agent.external` is the only place that uses mcp, uvicorn, starlette or httpx.
- The domain and the protocol record no telemetry. Tracing goes through `orbit.tracing`, and only
  the CLI uses `orbit.telemetry`, `orbit.profiling` and `orbit.client`.
- `orbit.execution` imports no orbit package, and neither the domain nor the protocol uses it.

A change that adds a package or an outside dependency adds its contract.

## Where new things go

- **A port**: `application/ports.py`, with its exceptions. A one-function port is a documented
  `Callable` alias there.
- **An adapter**: `orbit.agent.external`, named technology first. The CLI chooses and wires the
  adapters.
- **Start-up recovery, opening workspaces and policy**: the backend. The CLI only builds and
  starts.
- **A message between frontend and backend**: a model in `orbit.protocol`. A contract both sides
  share is written there once, so neither side guesses.
- **Presentation rules**: plain functions in `orbit.tui.view`; its controllers drive widget
  Protocols, and the widgets in `orbit.tui` lay out what they return.
- **Tests**: flat in `tests/`, with helpers in helper modules. Test modules never import each
  other; `tests/test_contracts.py` holds that.
