# Names

## Functions

- A function's name is a verb phrase saying what it does to produce its result: `parse_config`,
  `find_task`, `build_prompt`, `count_open_tasks`.
- Choose a specific verb: `parse`, `build`, `find`, `count`, `derive` and `render` say more than
  `get`, `do`, `handle` or `process`.
- A predicate reads as a yes-or-no question: `is_expired`, `has_changes`, `can_merge`.
- A function returns a result wherever it can. One that only acts, such as `close` or `send`, is
  named for the act.

## Variables, fields and properties

- They are nouns saying what they hold, in full words and the project's own terms: `config`,
  `tasks`, `readiness`.
- A collection takes a plural (`tasks`) and one item a singular (`task`).
- Name for meaning rather than type: `tasks`, not `task_list`. Choose specific nouns over `data`,
  `info`, `manager` or `util`.
- Ids end in `_id`: `task_id`.
- A state is named for what it describes (`readiness`), with an enumeration as its type (see
  `state.md`).

## Units

- Units live in types: the language's own where it has them (`timedelta`, `std::chrono::seconds`),
  and a units library otherwise (pint in Python, mp-units in C++).
- A name carries a unit only where no unit type reaches: a language without a units library, or a
  boundary where plain numbers cross, such as a config key (`timeout_minutes`) or a JSON field.
  The parser turns the number into the unit type as it arrives.

## Scope and change

- Name length follows scope: a short name only for a tiny scope, such as a comprehension's
  variable.
- Rename when the meaning changes.
- The language skill gives the case style.
