# Startup check in session views requirements

The decisions and plan for fixing two faults of phase 3b's sessions: the startup check blocked switching sessions, so a frontend picking a session at startup landed in a read-only view; and that view never showed what was running in the active session or when it finished.

## Goal

Picking a session at startup makes it the active one even while the startup check runs, and a read-only view always says what the active session is running and when it's free to switch.

## Behaviour

1. The startup check no longer blocks switching sessions, as before phase 3b: it runs on the base commit, not in a session, and a failure adds its fix draft to whichever session is active when it fails. `state` is `running` only for a task run, a review or a planner turn. This amends `docs/00010-feature-attach-and-lifecycle.md` item 6. Over keeping it blocking: every startup with checks lands read-only.
2. `SessionSummary` and `SessionStateChanged` gain `activity: tuple[Activity, ...]`, `Activity(kind, task_id)`: `kind` is `task_run`, `review`, `planner` or `startup_check`; `task_id` is set for task runs and reviews. A change in activity publishes `SessionStateChanged`. Over backend-written text: it can't use the TUI's task numbers or be parsed; over kinds only: vaguer.
3. A view's mark reads "viewing <name>; <active> is running <what> (enter to switch when it's idle)" while the active session is running, and "viewing <name>; <active> is idle (enter to switch)" otherwise; <what> is the TUI's wording of the activity ("task 3", "the review of task 5", "the planner", "the startup check", joined when several). The activation prompt names the same. Over the prompt only: the reason vanishes with it; over showing the active session's panels: two sessions' panels mixed.

   Amended: tasks go unnumbered, `a task` and `the review of a task`, not `task 3` and `the review of task 5`. The task list a read-only view shows is the viewed session's, and task ids are per-session counters, so its numbers named another session's tasks.

4. When the active session turns idle, a frontend that picked the session it views and was held back because the active one was busy gets the activation prompt again once: "<active> is idle now", "Make this the active session" preselected, and "Just look". Other viewers see only the mark change. Over the mark only: the user must notice; over switching automatically: breaks the rule that switching is explicit.

## Docs

5. The schema is regenerated; `docs/protocol.md` gains `activity`; README's Sessions section says the startup check doesn't block switching, and what the view's mark says.

## How it will be built

1. Write this record.
2. Let the startup check run without blocking switches; update `docs/00010-feature-attach-and-lifecycle.md` item 6.
3. Report what each session is running: `Activity` and `activity` on `SessionSummary` and `SessionStateChanged`.
4. Name the active session's activity in views: the mark and prompt wording, the re-offer when idle, README.
