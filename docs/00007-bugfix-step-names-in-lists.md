# Step names in lists requirements

The decisions and plan for fixing ambiguous lists of verification step names in what agents are told.

## Goal

Orbit joins step names with commas in the task brief, the `run_check` refusal and the planner's steps note. A step name containing a comma, such as orbit's own `Unit tests, 100% coverage`, then reads as two steps: agents call `run_check` with half the name, and the refusal repeats the same ambiguous list, so they keep guessing. Every list of step names an agent sees must show where each name begins and ends.

## Behaviour

1. One domain helper writes every list of step names an agent sees, each name in backticks (`` `Ruff lint`, `Unit tests, 100% coverage` ``); a name that itself contains a backtick is wrapped in double backticks with spaces, as Markdown does. The task brief, the `run_check` refusal and the planner's steps note all use it. Over one name per line: the brief and note grow by a line per step for no gain; over renaming alone: other projects keep the bug.
2. Orbit's own last verification step is renamed `Unit tests with 100% coverage` in `.orbit/config.toml`. Over leaving it: orbit's own config reads awkwardly in prose.
3. No user-visible change beyond those three messages' wording and the renamed step.

## How it will be built

1. Write this record.
2. Quote step names in lists agents see: `step_list` in `orbit.agent.domain.verification`, used by `task_brief`, the `run_check` refusal and `steps_note`.
3. Rename orbit's coverage step in `.orbit/config.toml`, with the README and the test that quote it.
