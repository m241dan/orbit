---
name: io-library
description: Building an I/O library, such as a protocol implementation, a database client or a service SDK — a Sans-IO core, thin adapters for each runtime, a shipped fake with contract tests, fuzzing for the parser, and the mistakes I/O libraries typically make. Use when writing, reviewing or planning code in a library whose job is talking to something outside.
---

# I/O library

An I/O library keeps its protocol pure and its I/O thin. It also takes the `library-api` skill.

## Shape

- **The core is Sans-IO** (Domain): the protocol as a state machine. It takes the bytes or events
  it is given and says what to send and what happened. It never waits; sockets, timeouts and flow
  control belong to the adapters.
- **Adapters** (External) connect the core to a runtime and let users choose: sync and asyncio in
  Python; ASIO with completion tokens in C++, so users pick callbacks, futures or coroutines.
- **A fake ships with the library**: an in-memory stand-in for the real service that passes the
  same contract tests as the adapters, so users never wrap the library.

## Placing code

- Protocol rules, framing, parsing and state transitions go in the core.
- An adapter only moves bytes and time between the core and its runtime, and translates transport
  errors into the library's own.
- Behaviour every adapter needs, such as retries or reconnecting, is written once: in the core
  when it is a protocol rule, as an Execution helper when it is about running.

## Writing tests

- The core: unit and property-based tests over bytes and events. Feed bytes and check the events;
  encoding then decoding gives back the input.
- Fuzz targets for the parser, since it reads untrusted input: libFuzzer in C++, Hypothesis in
  Python, with long runs in a later set.
- Adapters: medium tests against local servers over loopback or Unix domain sockets.
- Contract tests run over every adapter and the fake, so sync, async and fake behave alike.

## Typical mistakes

- I/O in the core: a socket read, a sleep, a clock.
- Protocol logic in an adapter, so the adapters drift apart.
- A fake that drifts from the real service.
- Timeouts and cancellation handled differently by each adapter.
