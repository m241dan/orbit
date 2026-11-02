---
name: cpp
description: C++ conventions and tools — C++23, CMake presets with vcpkg, a directory and CMake target per code type, Catch2 v3, std::expected and exceptions, snake_case with clang-format and clang-tidy, sanitizers, gcovr coverage, lizard limits, Mull, libFuzzer and RapidCheck. Use when writing or reviewing C++ code.
---

# C++

Adds C++'s tools and idioms to `code-style`.

## Build

- C++23, built with CMake presets. Dependencies come through vcpkg in manifest mode, pinned in
  `vcpkg.json`.
- Each check has a preset: a test build under AddressSanitizer and UBSan, a `tsan` build for the
  concurrency tests, and a `coverage` build that feeds gcovr.

Read `cpp`'s `build.md`: presets and dependencies.

## Layout and layers

- A directory per code type under `src/` (`domain/`, `application/`, `external/`, `execution/`),
  grouped under components when there are several. Entry points go in `apps/`, a library's public
  headers in `include/<project>/`, and the tests mirror `src/`.
- Each code type is a CMake library target, linked in the layer order, and each directory keeps
  to the include prefixes `include-rules.toml` allows it, which the include checker this skill
  ships, `scripts/check_includes.py`, checks. The linker never sees a header-only library such as
  ASIO, so the include checker is what keeps it in its layers.

Read `cpp`'s `build.md`: targets, includes and test labels.

## Suggested checks

Suggestions for the project's verification: the configurator proposes these, and the project's
.orbit/config.toml decides what runs. Warnings as errors, clang-tidy, formatting, function limits
of complexity 6, 100 lines and 5 parameters, the include checker, the tests under AddressSanitizer
and UBSan, and 100% line and branch coverage fit every task's checks; ThreadSanitizer, mutation
testing, fuzzing and benchmarks fit named sets. Mutation testing reports its failures to the lens
`tests-reviewer`, and benchmarks to `scaling-reviewer`.

Read `cpp`'s `checks.md`.

## Style

- snake_case for every name, template parameters included, like the standard library, Boost and
  ASIO; ALL_CAPS only for macros; a trailing underscore on private data members. The checked-in
  `.clang-format` and `.clang-tidy` hold the rest.
- Functions are verbs and variables nouns (`count_tasks()`, `tasks`), as `code-style`'s
  `naming.md` says. A type implementing a standard interface keeps that interface's names
  (`size()`, `begin()`).
- Values are small types passed by value or `const&`; member functions are `const` unless they
  change state; resources are owned through RAII. Units are types: `std::chrono` durations and
  mp-units quantities.

Read `cpp`'s `style.md`.

## Functional style

- Locals are `const` by default, and `constexpr` where they can be.
- Collections are built by ranges pipelines collected with `std::ranges::to`, inside small
  functions that return the value; fallible steps chain with `and_then`, `transform` and
  `or_else`.
- Where a bool would go, an `enum class` with named values.

Read `cpp`'s `functional.md`.

## Errors

- `std::expected<T, E>` for outcomes callers handle, with `E` the project's own error type and
  the function marked `[[nodiscard]]`.
- `std::optional` only for absence. A function that can fail returns `std::expected`, and
  `std::expected<void, E>` when success carries no value, as for the few functions with side
  effects.
- Exceptions for bugs and broken invariants.
- External turns library errors (`asio::error_code`, thrown exceptions) into the project's own.
  A port returns `std::expected`, and no exception crosses it.

Read `cpp`'s `errors.md`.

## Tests

- Catch2 v3. Test cases are named by sentences; Application tests use `SCENARIO`, `GIVEN`, `WHEN`
  and `THEN`.
- Contract tests run over a fake and its adapter with `TEMPLATE_TEST_CASE`.
- Asynchronous code is stepped by hand on the test thread (`run_one`, `poll_one`) with a fake
  clock.

Read `cpp`'s `tests.md`.
