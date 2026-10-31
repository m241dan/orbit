# Typing

- mypy is strict over `src` and `tests`. An escape is rare and local, and carries its code:
  `# type: ignore[override]`, `# noqa: E731`.
- **Ports**: a port with several methods is a `Protocol`; a one-function port is a documented
  `Callable` alias. Compose narrow Protocols into wider ones. Adapters and fakes implement them
  structurally, without inheriting. A class that needs a little from a sibling declares a narrow
  Protocol for it in its own module.
- **Aliases** are assignments with a docstring below: `Publish = Callable[[Event], None]`.
- **Generics** use PEP 695 syntax: `def first[T](items: Sequence[T]) -> T`, `class Held[T]`.
- **Ids** are `str`, named for their role.
- **Untyped data** is parsed into a model as it arrives. A raw payload that has to pass through
  is `object` (or `Mapping[str, object]`), narrowed with `isinstance`.
- **Optionals** are narrowed before use, in tests too. When the code knows a value is set:
  `assert x is not None  # why it holds`.
- **Closed unions** are matched with `match`, ending in `case _: assert_never(x)`. A catch-all
  default is for open sets.
- **Overrides** are marked `@override`, with mypy's `explicit-override` error code turned on.
- **Current forms**: `X | Y` unions, ABCs from `collections.abc`, `Self` returned from factories,
  and annotations without `from __future__ import annotations`.
