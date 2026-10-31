# State

## Where state lives

| Type | The state it holds |
|---|---|
| Domain | None. It computes the next state from the current one. |
| Application | The state of workflows and sessions. |
| External | Only its own system's connection state, such as a handle or a socket. |
| Execution | The state of tasks and schedules. |
| Entry | None, once the program has started. |

## One owner

- Each fact has exactly one owner. Every other part derives it or asks the owner, so no copies
  are kept in step by hand.
- Derive rather than store. A cache names its source and can be rebuilt from it.

## Changing state

- State is an immutable value. Its owner replaces it whole, so readers never see it half
  changed.
- Keep each value small enough to replace on every change: split large state among owners and
  key collections by id, so a change replaces only its part (see `performance.md`).
- Others change state only by asking its owner, through a command or a method.
- The next state comes from a Domain function of the current state and an event. Application
  applies it and carries out the effects, so every transition can be tested with a small test.

## Representing state

- A state is an enumeration or a union of variants. Anything that might be a bool is an
  enumeration with each value named, so a reader sees `readiness.ready` rather than `True`.
- A lifecycle is one value with a fixed set of states rather than several flags, so impossible
  combinations can't be represented.
- Data that belongs to one state lives in that state's variant (a `running` variant carries its
  start time), so it exists only while it means something.

## Lasting state

- State that must survive a restart is written whole (a temporary file, then a rename) and is the
  source of truth; everything else is rebuilt from it.

## Shared state

- State that concurrent work touches has one task that owns it, or a lock held across the check
  and the act together.
- After any wait, read the state again: it may have changed.
