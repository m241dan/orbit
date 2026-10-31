# Style

- **Names**: snake_case for every name, like the standard library, Boost and ASIO: types,
  functions, variables, namespaces, enumerators, constants and template parameters
  (`template <typename element, typename allocator>`). ALL_CAPS is for macros only. Private data
  members end in an underscore (`count_`). clang-tidy's `readability-identifier-naming` enforces
  all of it.
- **Function names** are verbs and variable names nouns, as `code-style`'s `naming.md` says:
  `count_tasks()`, `find_task()`, `is_empty()`. A type that implements a standard interface keeps
  that interface's names (`size()`, `begin()`, `end()`), so standard algorithms and ranges work
  with it.
- **Units** are types: `std::chrono` durations, and mp-units quantities for other units.
- **Where a bool would go**, an `enum class` with named values: `close(close_mode::force)`.
- **Private API**: in a library, everything outside `include/<project>/` is private, and private
  names inside public headers sit in a `detail` namespace.
- **Values** are small types passed by value or `const&`, with member functions `const` unless
  they change state. Locals are `const` by default (see `functional.md`). Change a shared value by
  making a new one; mutable state lives only with its owner (see `code-style`'s `state.md`).
- **Resources** are owned through RAII: `std::unique_ptr` for sole ownership, `std::shared_ptr`
  where ownership is truly shared, and raw pointers only to observe.
- **Threads and tasks** each have an owner that joins or stops them: a `std::jthread` with its
  `stop_token`, or an executor with a work guard.
- **Ports** are abstract classes with pure virtual member functions, owned by Application.
  External implements them, and tests implement fakes. A concept takes a port's place where the
  call is on a hot path.
- **Effects** come in as parameters: a clock as a callable or a clock type, randomness as a
  generator passed in. Domain receives the time and the random numbers it needs.
- **Parameter objects**: past five parameters, a struct of named fields, filled with designated
  initialisers at the call site.
- **Docs**: a `///` comment on each declaration in a header says in short plain English what it
  returns or does ("The tasks that …"), naming parameters in backticks.
- **Includes**: the file's own header first, then the project's, third-party and standard
  headers, as clang-format sorts them. Headers use `#pragma once`.
