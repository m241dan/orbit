---
name: code-style
description: How code is built here — a functional style, five code types (Domain, Application, External, Execution, Entry), where new code and dependencies go, how state is held, how things are named, how each type is tested, and the conventions for functions, errors, docs and telemetry. Use whenever writing, reviewing or planning code.
---

# Code style

The user's preferences for every project. A project's own skills add to these or replace them,
and a language skill (`python`, `cpp`) adds its tools.

## Functional style

- Functions take values and return values. A function that acts instead of returning a result is
  the exception, kept at the edges: in External, and in the workflows that call it.
- Data is immutable, local variables included: assign each name once, and give a new value a new
  name.
- Build a value in one expression, such as a comprehension, a ranges pipeline, or a chain of
  `and_then` and `transform`, rather than by changing a variable step by step. When the
  expression grows, move it into a small function that returns the value.

Read `code-style`'s `functional.md`.

## Five code types

Every piece of code is one of five types, each with its own rules and its own tests:

- **Domain** holds decisions, rules and transformations. Its results depend only on its inputs,
  so time, randomness and file contents arrive as parameters. Fast unit tests.
- **Application** holds the workflow. It sequences work, asks Domain for decisions, reaches
  outside through ports that External implements, and uses Execution for concurrency. Behaviour
  scenarios against fakes.
- **External** holds one thin adapter per outside system, turning library data and errors into
  the project's own types. Tests against local stand-ins: temp dirs, loopback, in-memory servers.
- **Execution** holds how work runs, knowing nothing of what the work is: task groups,
  supervision, cancellation, timeouts, retries. A deterministic loop and a fake clock.
- **Entry** builds, configures and starts everything, and reads like a list. A few start-up
  tests.

Imports point down: Entry, then External, then Application, then Domain, with Execution beside
Domain at the bottom. Domain and Execution import nothing of the project's. The project's map
says where each type lives; put new code where its type lives.

Read `code-style`'s `types.md`: how to place code, and each type's rules.

## Dependencies

Ask two questions of every new dependency:

1. Could a test that uses it be slow, flaky, or affected by the machine or by other tests? It
   has effects: use it only in External, behind an adapter, and give Application's tests a fake.
2. Would a test need to run a loop or wait for it to call back? It takes control: use it from
   Execution, Application and External, and run it deterministically in tests.

Split a dependency that does both along those lines; use one that does neither anywhere. Then
add its rule to the import contracts. The clock, randomness, ids and environment variables have
effects too: Domain receives them as parameters, and elsewhere they come in as parameters
wherever a test needs to control them.

Read `code-style`'s `dependencies.md`: examples of each kind of dependency.

## State

- Each fact has one owner, and every other part derives it or asks the owner.
- State is an immutable value that its owner replaces whole. The next state comes from a Domain
  function of the current state and an event; Application applies it and carries out the
  effects.
- State is an enumeration or a union of variants, including anything that might be a bool, so
  every value has a name and impossible combinations can't be represented.

Read `code-style`'s `state.md`.

## Names

- Functions are verbs saying what they do to produce their result: `parse_config`, `find_task`.
- Variables, fields and properties are nouns saying what they hold: `config`, `tasks`.
- Units live in types (`timedelta`, `std::chrono::seconds`, a units library). A name carries a
  unit only where plain numbers cross a boundary, such as a config key.

Read `code-style`'s `naming.md`.

## Tests

Write the test first and watch it fail. Cover every line and branch with a test that checks
behaviour, and type tests like the code; the coverage check that enforces it is the language
skill's suggestion. Keep each test independent of the machine and of other tests: pass in the
clock and randomness, drive loops by hand, and give External's tests local stand-ins such as temp
dirs, local git and loopback.

Read `code-style`'s `testing.md`: testing each type, fakes, mutation testing and property-based
tests.

## Performance

- Know the sizes each hot path will see, and keep work in proportion to the change: update what
  changed, look things up by key, batch I/O.
- Keep immutable values small: split large state among owners, and replace only the part that
  changed.
- Put a limit on everything that grows, and measure before optimising; a change made for
  performance cites its measurement.

Read `code-style`'s `performance.md`.

## Functions and errors

Keep every function, tests included, to cyclomatic complexity 6, 100 lines and 5 parameters, and
split it well before it reaches them; past five parameters, bundle them into one object. A CLI
command or an Entry function may pass the parameter limit with its reason written beside it.
The checks that enforce these limits are the language skill's suggestions. Every port declares
its own errors, and External translates OS and library errors into them; the language skill says
how errors are expressed.

An optional means a value or nothing, never success or failure. A function that can fail says so
in its return type, with an expected or a result variant, even when success carries no value.
Those are rare: only the few functions with side effects return nothing on success. A search that
finds nothing returns an empty optional; a parse that fails returns its error.

Read the language skill's `errors.md`: how errors are expressed in that language.

## Changing code

Before handling a case, find how sibling paths handle it and match them; when fixing a bug, fix
every path with the same pattern. When a feature grows from one case to many, find every place
that still assumes one. Pin each fix with the test that would have caught it, in the fix's own
commit.

## Docs, comments and telemetry

Each function's doc says in short plain English what it returns or does ("The tasks that …").
Comments are rare and explain why. A change users can see updates the README in the same change.
Record one wide span per unit of work, with errors on it.

Read `code-style`'s `writing.md` and `telemetry.md`.
