# Models and data

- **Pydantic where data crosses a boundary as text**: wire messages, config files, JSON saved to
  a store, JSON output. Each family of models shares one base with
  `ConfigDict(frozen=True, extra="forbid")`; `extra="ignore"` only where a file holds other
  sections too.
- **Files a user writes** add `strict=True`, so a typo or a wrong type fails loudly. Wire models
  follow the protocol's own rules.
- **Validate declaratively**: constraints and user-facing descriptions go in
  `Field(ge=1, description=…)`, reused constrained types are `Annotated` aliases, and a validator
  delegates to a Domain rule function. A `ValidationError` becomes one project error with a
  problem per line.
- **Every other value is a frozen dataclass.** Change one by copying it: `dataclasses.replace`, or
  `model_copy(update=…)` for pydantic. Hold state in a plain class with private attributes that
  holds frozen values.
- **Result variants** are frozen dataclasses joined by a union alias
  (`UsageFetch = UsageReport | UsageRateLimited | UsageUnavailable`), branched on with `match` or
  `isinstance`.
- **Closed sets**: a `Literal` for a closed set of values, read at runtime with `get_args`; an
  `Enum` for a closed set used only inside the code (`StrEnum` when the value is display text).
  An open set that a newer peer may extend is a `str` alias with its known values named as
  constants. Anything that might be a bool is one of these, with its values named.
- **Collections**: accept the abstract read-only type (`Sequence`, `Mapping`, `Iterable`), return
  the concrete one, and hold tuples in frozen values.
- **Constructors**: past five parameters, bundle collaborators into a frozen `XParts` dataclass and
  callbacks into a frozen `XHooks` dataclass with no-op defaults. Give each optional collaborator
  a null-object default (`NoTelemetry()`), or `None` meaning "not configured". Store
  collaborators in private attributes and expose read-only properties.
- **Effects** come in as callables with real defaults: `now: Callable[[], datetime] = utc_now`,
  `new_id: IdMaker = new_session_id`.
