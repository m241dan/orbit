---
name: application
description: Building an application, a program with users, workflows and outside systems — all five code types, ports and adapters, Entry wiring real adapters while tests wire fakes, modes and partial failures, user interfaces, and the mistakes applications typically make. Use when writing, reviewing or planning code in an application.
---

# Application

An application has all five code types. Domain holds its rules, Application its workflows,
External one adapter per outside system, and Execution a few helpers. Entry builds it with the
real adapters; the tests build it with fakes.

## Placing code

- Place each piece with `code-style`'s procedure, where the project's `layers` map says its type
  lives.
- What a workflow needs from outside is a port, kept with Application along with the port's
  errors. Each outside system gets one adapter in External, and Entry chooses and wires them.
- Decisions and user-facing wording are Domain functions; a workflow asks for them.
- Entry builds and starts. Opening resources, start-up recovery and policy belong to the
  workflows Entry starts.
- A contract two sides share, such as a wire protocol or a file format, is written once, in a
  module both import.

## Adding a dependency

Place it with the two questions. An effectful one gets a port, an adapter, a fake, and contract
tests that the adapter and the fake both pass; an execution-model one is used through
Execution's helpers. Add its line to the import contracts.

## Writing tests

- Domain: small synchronous tests, with tables of cases.
- Application: scenarios through the public surface, commands in and events out, with a fake for
  every port.
- External: real things run locally, asserting the translated error and that nothing was left
  half done.
- Entry: a start-up test that builds the real wiring.
- Every fake passes the contract tests of its adapter.
- Hot paths the application names, such as the work done on every event, keep benchmarks in the
  benchmark check set.

## Modes, steps and growth

- Before code starts work or accepts a command, check every mode that can forbid it: halted,
  read-only, paused, closing, disabled. When the mode lifts, restart or re-arm what it held.
- Order a multi-step change so that a failure partway leaves state a retry can finish: set flags
  and delete records after the steps that can fail, or undo the earlier steps.
- When a feature grows from one case to many, such as one user to many or one workspace to many,
  find every place that still assumes one.

## User interfaces

An application with a user interface also follows its rules.

Read `application`'s `ui.md`.

## Typical mistakes

- Logic in an adapter or in Entry, where no fast test reaches it.
- Domain reading the clock or the environment instead of receiving them.
- Mocks of libraries the project doesn't own, in place of fakes of its own ports.
- Fakes drifting from their adapters for want of contract tests.
- A case a sibling path handles, missed on a new path.
- State read before an `await` and trusted after it.
