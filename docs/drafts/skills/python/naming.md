# Names

Names follow `code-style`'s `naming.md`: functions are verbs, and variables, fields and
properties are nouns. In Python:

- **Case**: snake_case for functions, variables and modules; CapWords for classes and type
  aliases, as PEP 8 says.
- **Functions**: `parse_config`, `find_task`, `build_prompt`; a predicate as a question:
  `is_expired`, `has_changes`.
- **Where a bool would go**, a `Literal` alias or an `Enum` (see `models.md` for which), so a call
  reads `close(mode="force")`.
- **Exceptions**: `…Error` for invalid input (`ConfigError`); a name for what happened when an
  operation fails (`TreeFailed`, `HandshakeRefused`, `TokenRejected`).
- **Rule checks**: a `check_*` function raises when its rule fails; a function that returns the
  refusal instead is named for finding it (`find_merge_refusal`).
- **Adapters** put the technology first (`GitCoderTree`, `SqliteStore`), so an adapter never
  shares its port's name.
- **Parameter objects**: `XParts` holds a constructor's collaborators and `XHooks` its callbacks,
  both frozen dataclasses.
- **Ids** are `str`, named for their role: `task_id`, `session_id`.
- **Private names**: a leading underscore means private to the module. A name used outside its
  module is public, and tests import only public names.
- **Tests** are named by sentences; see `tests.md`.
