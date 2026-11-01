# Triggers requirements

The decisions and plan for milestone 12a of `scratch/control-plane-design.md`: what starts an
agent becomes configuration. A trigger turns an event into a task for a role, or into a run of
the verification steps, and the shipped triggers reproduce what orbit does today.

## Goal

Reviews, the super review, the startup check and its fixes, and the configurator start from
triggers in configuration rather than from lane code, so a project can change them or add its
own without changing orbit. Opening a workspace runs its verification, or sets it up with the
user when there's none, and tasks don't run until there is some.

## Milestone 12's phases

1. Milestone 12 is planned in five phases, each when the one before lands, with its own record:
   12a triggers, 12b check sets (sets that gate or report, suggested checks in skills, the
   configurator proposing sets, the C++ include checker), 12c merge holds, 12d lens reviewers
   (one per lens as the default, the merge pass, tabs grouped by trigger), 12e reporting sets and
   schedules; over two large plans or one.
2. The shipped configuration reproduces today; 12a ships today's single reviewer, and 12d makes
   per-lens reviewers the default.

## Behaviour

3. `[triggers.<name>]` has `on`, `role`, `prompt` (a template file under `.orbit/` or inline
   text), `when`, `guards`, `every`, `manual` with a `label`, and `enabled`; shipped triggers
   live in the package, a project overrides their fields or adds its own, and the user config
   can't set triggers; over personal triggers in the user config (they'd differ by machine).
4. `super_review_every` stays for one version as an alias for the `super-review` trigger's
   `every`, with a notice to move it; with both set, the trigger wins; over refusing it (every
   existing config would fail to load).
5. `session_started` fires when orbit opens a workspace with its session, not on a session
   switch. With steps configured and verification on, it runs them, as today's startup check
   does, and a failure queues a fix; with none, it starts the configurator, which works with the
   user.
6. A trigger queues a role's task or runs checks (`checks = "gate"`); a check run shows as today,
   in the verification panel and as `checker` under All, not on the task list; over check runs
   as `checker` tasks (the list would fill with check runs).
7. Until the project has a verification step, running tasks is refused; the configurator's task
   never is, conversations stay open, and with `--no-verify` nothing is refused; over locking
   conversations too.
8. ctrl+g and ctrl+e stay bound to `super-review` and `configure-checks`; ctrl+p lists every
   manual trigger by its label and fires the one picked; over keys only (a trigger couldn't be
   fired until bound) and Textual's palette (its look and keys differ).
9. A trigger never fires on an event caused by a task it queued itself; over refusing such
   triggers at load (it rules out legitimate chains) and an hourly cap (loops up to the cap).
10. No `task_started` event.

## Edge cases

11. - Switching to another session in an open workspace: nothing re-runs.
    - Steps configured but `--no-verify`: nothing runs, and tasks aren't refused.
    - No steps, and the configurator ended without accepting checks: tasks stay refused; ctrl+e
      starts it again, and the refusal says so.
    - A task started while the configurator works: refused, `Set up verification first: the
      configurator is working on it.`
    - The configurator's own task: never refused.
    - The startup run fails: a fix is queued as today, and tasks aren't refused.
    - Steps added by hand while orbit is open: they count once the workspace opens again.
    - A trigger naming a role, event or guard that doesn't exist: a configuration error at load,
      naming the trigger and the value.
    - A trigger whose role is disabled: never fires; a manual one leaves ctrl+p's list and its
      key.
    - `enabled = false`: never fires, and isn't listed.
    - A template using a variable its event doesn't give: a configuration error at load, naming
      the trigger and the variable.
    - Two triggers on the same event: each fires, in configuration order.
    - A trigger firing while its role's lane is busy: its task waits on the board.
    - A trigger firing while tasks are refused: its task is refused like any other, with a
      notice naming the trigger; the configurator's never is.
    - ctrl+p on a manual trigger whose template takes input: fired with empty input for now.
    - `super_review_every` and the trigger's `every` both set: the trigger's wins, with the
      notice.
    - A project overriding a shipped trigger's `prompt`: its template replaces the shipped one
      whole.
    - A session saved with origin `super_review`: read as the trigger `super-review`.

## Decided for you

12. Events and their `when` fields: `session_started` (`verification`: `configured`, `missing`
    or `off`); `task_landed` (`role`, `origin`); `task_finished` (`role`, `origin`, `status`);
    `commits` (counting landings that match `when` on `role` and `origin`, firing every
    `every`); `startup_check_failed` (one per failed step, `step`); `manual`. `batch_landed`
    waits for 12d; until then the `review-rounds` guard keeps today's batching inside the review
    trigger; over every event of the design now.
13. Template variables: every event `{trigger}`; `task_landed` `{commit}`, `{task}`, `{title}`,
    `{diff}`, `{missing}`, `{cap}`, `{queued}`, `{landed_since}`, `{task_skills}`; `commits` and
    the manual super review `{commit}`, `{cap}`, `{queued}`, `{since_commits}`,
    `{changed_files}`, `{sample}`; `startup_check_failed` `{step}`, `{output}`. A list-like
    variable renders as its block or as nothing, and a paragraph left empty is dropped, so the
    shipped templates reproduce today's prompts exactly; over raw lists templates must format.
14. Shipped triggers, in the package's `triggers.toml` with templates beside it: `review`
    (`task_landed`, guard `review-rounds`, role `reviewer`); `super-review` (`commits`,
    `every = 10`, `when = { origin = ["manual", "planner"] }`, guard `suggestions-pending`,
    `manual = true`, label "Super review", role `super-reviewer`); `configure-checks` (manual,
    label "Set up checks", role `configurator`); `set-up-verification` (`session_started` when
    `verification = "missing"`, role `configurator`); `startup-check` (`session_started` when
    `verification = "configured"`, `checks = "gate"`); `startup-fix` (`startup_check_failed`,
    role `coder`, `draft = true`). Guards: `review-rounds`, `suggestions-pending`, `not-queued`,
    `dedupe`. Fallback fix tasks stay lane code.
15. A triggered task's origin is its trigger's name; a saved `super_review` reads as
    `super-review`; `orbit.trigger` on spans is the trigger's name.
16. Refusals: `Set up verification first: the configurator is working on it.` while it runs,
    otherwise `Set up verification first: press ctrl+e.`; a refused triggered task gets a notice
    naming its trigger; adding and accepting tasks stay allowed, and only running is refused.
    `FireTrigger` with input is still refused in 12a.
17. `[triggers.*]` leaves the reference config's later section: the reference shows the shipped
    triggers commented out, as it shows roles, and `orbit config show` lists
    `triggers.<name>.<field>` with their layers. The README describes triggers, events, guards,
    variables, ctrl+p and the refusal.

## How it will be built

1. Write this record.
2. Load triggers from configuration: `Trigger` in `orbit.agent.domain.triggers`, the package's
   `triggers.toml` and templates, the project layer, validation, the `super_review_every`
   alias, the reference config and `config show`; nothing fires yet.
3. Fire triggers on events, starting with reviews: `TriggerEngine` in
   `orbit.agent.application.triggers`, `task_landed` and `task_finished`, guards, rendering, the
   self-trigger rule, origins and spans; the `review` trigger replaces the hard-coded review.
4. Move the super review onto its trigger: `commits`, `suggestions-pending`, manual firing,
   `super_review` read as `super-review`.
5. Move the startup check and its fixes onto triggers: `session_started`,
   `startup_check_failed`, `checks = "gate"`, `startup-fix` as a draft.
6. Start the configurator when verification is missing: `set-up-verification`,
   `configure-checks` through the trigger, running tasks refused until a step exists.
7. List and fire manual triggers: the Catalog and `FireTrigger` naming configured triggers,
   ctrl+p's picker, ctrl+g and ctrl+e by name.
