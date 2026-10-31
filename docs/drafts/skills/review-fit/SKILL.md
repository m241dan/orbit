---
name: review-fit
description: Review lens for one commit — does the change do what its task and the plan's record asked, no more and no less, with the README updated for anything users see? Use when reviewing a change against the task that asked for it.
---

# Fit lens

**Your question:** does this change do what its task asked, no more and no less?

## Look for

- Behaviour the task asked for that is missing or only partly there.
- Done items met on one path but not on every ending path and call site.
- Decisions in the plan's record that the change ignores or contradicts.
- Work the task didn't ask for: changes in files it didn't need, features beyond its scope.
- A change users can see (a key, a flag, a config setting, a role) without its README update.

## Measure against

The task's prompt, its Keep and Done parts where it has them, and the plan's record it names.

## Leave to the other lenses

- Something the task said to keep, now broken: that's a regression, so Bugs reports it.
- Whether tests pin the behaviour down: Tests.
- Whether each piece is the right code type: Logic.

## A finding

Quote what the task asked, say what the change does instead, and point to where. Fit findings
never block merges.

Most changes fit their task. When this one does, report no findings.
