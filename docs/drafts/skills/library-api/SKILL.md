---
name: library-api
description: The public API of a library — what is public, how it is versioned and deprecated, how compatibility with the last release is checked, documented errors, examples run as tests, and test doubles shipped to users. Use when writing or reviewing the public surface of any library.
---

# Library API

A library's public API is its contract with every user. Keep it explicit, stable and tested.

## What is public

- Python: the names a package re-exports in its `__init__` and lists in `__all__`. Everything
  else is private.
- C++: the headers under `include/<project>/`. Everything in `src/` or in a `detail` namespace is
  private.
- Public functions take and return the library's own types or standard ones, so internal types
  stay inside.

## Versions

- SemVer. Before 1.0, a minor version may break the API.
- After 1.0, a breaking change waits for a major version, and the minor release before it warns:
  `@warnings.deprecated` in Python, `[[deprecated]]` in C++, each naming the replacement.
- Each release lists its changes, breaking ones first.

## Compatibility checks

- Python: `griffe check` against the last release.
- C++: the last release's tests and examples compiled against the new headers, and libabigail's
  `abidiff` for shared libraries.
- The slow comparisons run in a later check set.

## Errors and docs

- The errors a caller handles are part of the API: the library's own types, documented and
  versioned like the rest.
- Every public name is documented, and the examples in the docs run as tests.

## Benchmarks

A library keeps benchmarks for its hot paths in the benchmark check set, so a slowdown shows
before a release.

## Test doubles for users

A library with effects, or one that takes control, ships what its users need to test without it:
a fake that passes the same contract tests as the real thing, or a deterministic executor and a
fake clock. The kind skill says which.
