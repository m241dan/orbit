---
name: execution-library
description: Building an execution library, such as a scheduler, an actor system or an async framework — Execution as the product with the five code types inside it, deterministic tests with a hand-driven loop and a fake clock, stress tests under ThreadSanitizer, model checking for lock-free code, test hooks shipped to users, and the mistakes execution libraries typically make. Use when writing, reviewing or planning code in a library about how work runs.
---

# Execution library

In an execution library, Execution is the product, and the five code types repeat inside it. It
also takes the `library-api` skill.

## Shape

- **Domain**: scheduling rules, priorities, and the state machines of futures and promises, as
  plain logic a single thread can test.
- **Application**: the orchestrator that maps the library's API onto its engine, such as ASIO:
  wrapping work in handlers, choosing strands, dispatching to executors.
- **External**: the parts that reach the OS, such as sockets, timers and the reactor, usually
  through the engine.
- **The API** lets users choose how they wait. On ASIO, completion tokens give callbacks, futures
  or coroutines.
- **Test hooks ship with the library**: a deterministic executor and a fake clock, so users test
  their own code on it without threads or real time.

## Writing tests

- **Deterministic first**: drive the loop by hand on the test thread (`run_one`, `poll_one`, a
  single-threaded executor) and advance a fake clock. These are small tests, run in the gate.
- **Stress tests** run under ThreadSanitizer, AddressSanitizer and UBSan with many threads and
  iterations, in a later check set. Each prints the seed or schedule that replays a failure.
- **Lock-free code** gets a controlled scheduler or a model checker such as Relacy. Random delays
  only make a race likely, so a passing stress run proves nothing.
- **Mutation testing** covers the Domain rules.

## Facts worth keeping straight

- `poll()` runs every ready handler; `poll_one()` runs one.
- `run_one()` is deterministic only with a fake clock; timers read the real clock otherwise.

## Typical mistakes

- Races on shared state, and state that changed while a handler waited.
- A handler outliving the object it captured: keep the object alive for as long as its handlers
  can run.
- Cancellation that leaves an operation half done, or a handler that runs after its cancel.
- Handler ordering assumed where only a strand guarantees it.
- A blocking call inside a handler, stalling the loop.
