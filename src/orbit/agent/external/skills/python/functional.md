# Functional style in Python

- **Single assignment**: assign each local once, and give a new value a new name. Python can't
  enforce it, so the Style lens checks it.
- **Build with expressions**: comprehensions and generator expressions for collections,
  conditional expressions for choices, and `any`, `all`, `sum`, `min` and `max` over generators
  for reductions. Return tuples and frozensets for values that won't change.

  ```python
  def collect_open_titles(tasks: Sequence[Task]) -> tuple[str, ...]:
      return tuple(task.title for task in tasks if task.state == "open")
  ```

- **Change by copying**: `dataclasses.replace(task, state="done")`, or `model_copy(update=…)` for
  pydantic.
- **Results** are variants: frozen dataclasses joined by a union, branched with `match` and closed
  with `assert_never`.
- **Where a bool would go**, a `Literal` alias or an `Enum` with each value named.
- **Functions as values**: pass a key, a predicate or a transformation (`sorted(tasks, key=…)`),
  and fix arguments with `functools.partial`.
- **Effects at the edges**: a workflow builds the whole result with pure functions, then hands it
  to External in one call.
