# Writing

## In code

- Each function's doc says in short plain English what it returns or does: "The tasks that …",
  "Whether …", "Starts …". The language skill gives the format.
- Comments are rare and explain why: a constraint, a trade-off, a surprise. The code says what.
- Names follow `naming.md`, in the words the project already uses for each idea.
- Order a module top-down: the public function first, then the helpers it calls.

## For users

- A change users can see, such as a key, a flag, a config setting or a role, updates the README
  in the same change.

## For agents

Skills, role instructions and prompts are read by agents:

- Say what to do, as an instruction: "Pass the clock in as a parameter."
- Give the reason in a few words when it isn't obvious.
- Prefer concrete examples to abstract rules.
