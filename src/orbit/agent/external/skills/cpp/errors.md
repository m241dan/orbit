# Errors

- **Outcomes callers handle** return `std::expected<T, E>`, where `E` is the project's own error
  type: an `enum class` naming what happened, or a small struct that adds context. Such functions
  are `[[nodiscard]]`.
- **`std::optional` means present or absent**, never success or failure: a search that finds
  nothing returns `std::nullopt`. A function that can fail returns `std::expected`, so its failure
  has a type and a reason.
- **Success without a value** is `std::expected<void, E>`. It is rare: most functions return a
  value, and only the few with side effects return nothing on success.
- **Bugs and broken invariants** throw a project exception named for what happened.
- **A type whose construction can fail** for an expected reason gets a factory function returning
  `std::expected`, so its constructors succeed or throw only for bugs.
- **Ports return `std::expected`.** External catches the library's exceptions and turns
  `asio::error_code` and other library errors into the project's error type, naming the file,
  setting or flag that caused it. No exception crosses a port.
- **Translating keeps the cause**: carry the original error's code or message in the new error's
  context.
- **Handling**: deal with a `std::expected` where it arrives, by returning early on an error or
  chaining with `and_then`, `transform` and `or_else`.
