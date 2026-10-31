# Functional style

## Functions

- A function takes values and returns a value. It changes nothing it was given, and depends on
  nothing it wasn't given.
- A function that exists for its effect, such as writing a file or sending a message, lives at
  the edges: in External, or in the Application workflow that calls it. Compute everything it
  needs first, with pure functions, then hand the result over in one call.
- Write rules as plain functions, and give state and services classes.
- Compose small functions rather than writing one long one: a pipeline of steps, each a named
  function.
- Pass functions as values where a step varies: a key, a predicate, a transformation.
- Handle every case: a function over a closed set of variants matches each one, and the type
  checker or compiler confirms none is missed.

## Data

- Data is immutable, local variables included. Assign each name once; a new value gets a new
  name.
- Change a value by building a new one from it, such as a copy with one field replaced.
- Use immutable collections for values that don't change.
- Mutable state lives only with its owner (see `state.md`).

## Building values

- Build a value in one expression rather than step by step: a comprehension, a ranges pipeline
  collected into a container, a conditional expression.
- Chain fallible steps rather than nesting checks: `and_then`, `transform` and `or_else` on
  optional and expected values, or a `match` over result variants.
- When an expression grows past a line or two, move it into a small named function that returns
  the value. Such functions stand in for comprehensions where a language lacks them.

## Types

- Make impossible states unrepresentable: enumerations and unions of variants rather than flags,
  and required fields rather than optional ones that must be set together.
- Where a bool would go, in state, fields or parameters, use an enumeration with each value
  named, so a call reads `close(close_mode.force)` rather than `close(True)`. A predicate may
  still return a bool for a condition to test at once.
- An optional means a value or nothing, never success or failure. A function that can fail
  returns an expected or a result variant, so its failure has a type and a reason. When success
  carries no value, as for the few functions with side effects, the success side is empty
  (`std::expected<void, E>` in C++).

## Each language

- Python: comprehensions and generator expressions, conditional expressions, frozen dataclasses
  and `dataclasses.replace`, `match` over unions; see the `python` skill's `functional.md`.
- C++: ranges pipelines collected with `std::ranges::to`, algorithms over raw loops, `const` and
  `constexpr`, the monadic operations of `std::optional` and `std::expected`, `std::variant` with
  `std::visit`; see the `cpp` skill's `functional.md`.
