---
name: pure-library
description: Building a pure library — mostly Domain code with no I/O and no Entry, results that depend only on inputs, few and pure dependencies, property-based and mutation tests, and the mistakes pure libraries typically make. Use when writing, reviewing or planning code in a library that does no I/O, such as a parser or a math library.
---

# Pure library

A pure library is mostly Domain code. It has no External, because its callers bring the I/O, and
no Entry, because the caller's `main` is the Entry. A small Application may sequence its internal
steps. It also takes the `library-api` skill.

## Placing code

- Rules and transformations are Domain functions over immutable values.
- Anything that would read the clock, randomness, the environment or a file becomes a parameter,
  so every result depends only on the inputs.
- State a caller keeps between calls is a value the library returns and takes back, rather than
  something the library holds.

## Adding a dependency

Take only pure dependencies, as few as do the job: each one becomes a dependency of every user
too.

## Writing tests

- Unit tests for each rule, with tables of cases.
- Property-based tests for every rule that should hold across inputs (Hypothesis, RapidCheck):
  round trips, orderings, invariants.
- Mutation testing over all of it, since all of it is Domain.
- The examples in the docs run as tests.

## Typical mistakes

- Hidden effects: a global, a cache shared between calls, the clock.
- A breaking change without a release that warns first.
- Internal types leaking into the public API.
- A dependency's errors passing through unwrapped, in place of the library's own.
