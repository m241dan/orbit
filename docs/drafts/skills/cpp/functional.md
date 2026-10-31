# Functional style in C++

- **Locals are `const`** by default, and `constexpr` where they can be. clang-tidy's
  `misc-const-correctness` flags locals that could be `const`.
- **Build collections with ranges**: a pipeline of views collected with `std::ranges::to`, inside
  a small function that returns the value. These functions stand in for list comprehensions.

  ```cpp
  std::vector<std::string> collect_open_titles(std::span<const task> tasks) {
      return tasks
          | std::views::filter([](const task& t) { return t.state == task_state::open; })
          | std::views::transform([](const task& t) { return t.title; })
          | std::ranges::to<std::vector>();
  }
  ```

- **Algorithms over raw loops**: `std::ranges::find_if`, `any_of`, `count_if` and `fold_left`
  name what a loop would only show.
- **Chain fallible steps** with the monadic operations of `std::optional` and `std::expected`:

  ```cpp
  std::expected<config, config_error> make_config(std::string_view text) {
      return parse_config(text)
          .and_then(validate_config)
          .transform(apply_defaults);
  }
  ```

- **Closed unions** are `std::variant`s, handled with `std::visit` and an overload set that
  covers every alternative, so the compiler confirms none is missed.
- **Where a bool would go**, an `enum class` with named values: `close(close_mode::force)`.
- **Shared helpers**: comprehension-like helpers the standard library lacks live in one small
  header of pure functions, named for what it holds. It includes nothing else of the project, so
  any code type may use it, as it would a pure library.
- **Effects at the edges**: compute the whole result with pure functions first, then hand it to
  External in one call.
