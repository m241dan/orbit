# Task list requirements

The agreed requirements for making orbit's task list follow the task that's running, and what is
deliberately left out. This is the source of truth for the work that follows. README's
[Tasks](../README.md#tasks) section says how the task list works today.

## Goal

A long task list shouldn't hide the work in progress: the list keeps the running task in view by
itself, until you take it over, and lets go again when you're done looking.

## Active task

- The active task is the topmost task on the list whose status is `running` or `verifying`: coder
  tasks and reviews alike, in any role.
- If no task is running or verifying, there is no active task.

## Following

- The list follows by default: at startup, and after switching sessions.
- While following, the list scrolls just enough to keep the active task in view, with one row of
  context above it where there's room.
- It scrolls only when the active task would otherwise be out of view, so it doesn't jump on every
  change.
- With no active task, a following list stays where it is.

## Activating

- `enter` with the Task list window selected activates the list as today, and puts the highlight
  on the active task if there is one, otherwise on the first row.
- Activating holds the list (see below).

## Held

- A held list doesn't scroll by itself, and its title reads `Task list · held`.
- Activating the list holds it, and so does scrolling it with the mouse wheel. Orbit's own follow
  scrolling never holds it.
- The first `esc` leaves list mode as today; the list stays held and keeps its highlight.

## Letting go

- `esc` with the Task list window selected and nothing active (normal mode) scrolls to the active
  task and follows again; the title drops `· held`.
- This is the "double escape", and mirrors the conversation's held entry and its second `esc`.

## How it will be built

1. The active-task view rule, follow scrolling, and activation landing on the active task.
   Done.
2. Holding (on activating and on the mouse wheel), the `· held` title, and `esc` letting go.

## Out of scope

- A timed double-press of `esc`: the two presses are separate steps, however far apart.
- Following while the sidebar is hidden needs no special case; the list just keeps following.
