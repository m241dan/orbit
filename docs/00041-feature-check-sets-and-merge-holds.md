# Check sets and merge holds requirements

The decisions and plan for milestones 12b and 12c of `scratch/control-plane-design.md`, after
`docs/00040-feature-triggers.md`: verification steps grouped into named sets that triggers run,
sets that hold merges while they fail, skills that suggest checks rather than state them, the
configurator proposing sets, and the C++ include checker.

## Goal

Quick checks run in every task, as today; slow checks run as named sets on a cadence. A set that
holds merges stops work landing on the user's branch while it fails, until a fix lands and the set
passes again. The configurator sets this up with the user from the language skills' suggestions.

## Sets

1. A step joins a named set with `set = "<name>"`; steps without one are the **task checks**, run
   by every task and the startup check as today; "gate" becomes "task checks" in the README and
   the configuration's descriptions; over lists of sets per step (it blurs what every task runs)
   and set tables naming their steps (a step's set written away from it).
2. A named set holds merges when it fails only with `[verification.sets.<name>] holds_merges =
   true`; otherwise it reports; task checks never hold merges; over saying it on each trigger
   (one set behaving differently by trigger).
3. A trigger runs a set with `checks = "<set>"`, on any event.

## Holds

4. During a hold, coders keep working and their commits queue for the user's branch; the fix lands
   with the commits stacked beneath it (those finished before it started), and commits finished
   after it wait; over idling the work lane, stopping coders (work lost), landing the fix alone
   (it may depend on or conflict with what's beneath it) and a second worktree for holds.
5. A hold lifts once the fix has landed and the failing set, re-run on the new commit, passes;
   over lifting as soon as the fix lands (a fix that didn't work would release merges).
6. The user can release a hold by hand with ctrl+f, after a confirmation (ctrl+h is backspace in
   most terminals); over no manual release (a flaky set could stall the project).
7. A reporting set's failure gives a notice, its output in the verification panel; nothing is
   queued; over a draft fix each time (drafts would pile up).

## Setup

8. Language skills suggest checks rather than state them; the configurator proposes task checks
   plus slower named sets with the triggers that would run them, from those suggestions and what
   the project already uses, and the user accepts, changes or drops each; over task checks only.
9. The C++ include checker ships as a script of the `cpp` skill and runs without orbit; it reads
   `include-rules.toml` at the project root, whose `[rules]` map each directory to its allowed
   include prefixes; over a table in `.orbit/config.toml` (CI would read orbit's config) and
   CMake comments (scattered, fragile).
10. It counts `"…"` and `<…>` includes, matches prefixes by path segment, allows standard headers
    unless a directory's rules name `std`, gives a directory its nearest listed parent's rules and
    fails one with none, lists every violation, and exits 1 if there are any; over leaving
    unlisted directories unchecked.

## Edge cases

11. - A trigger whose `checks` names a set with no steps, or `[verification.sets.x]` for a set
      with no steps: a configuration error at load.
    - A named set no trigger runs: allowed, with a notice at open: `The set mutation has no
      trigger, so it never runs.`
    - A set that holds merges fails: merges hold; a fix task goes to the front of the queue,
      naming the commits since the set last passed and carrying its output, and lands despite the
      hold.
    - The re-run after the fix fails: the hold stays and another fix is queued, up to
      `fallback_fixes`; past that, the hold stays and a notice asks the user to step in.
    - Two sets hold: merges wait until both lift; each has its own fix.
    - A set's trigger fires while it runs: one more run on the latest commit, later firings
      folding into it.
    - Orbit restarts during a hold: the hold is restored, saved per repository.
    - A hold released while merges wait: they land in queue order, conflicts handled as today.
    - While held: the task list's title reads `Task list · merges held: stress failed`, rows whose
      work waits show `waiting to merge`, and a notice in All says when it starts and lifts.
    - A set runs in a read-only snapshot of the latest commit, leaving the coder's worktree alone,
      shown in the verification panel under its name.
    - `--no-verify`: sets don't run and nothing holds.
    - A reporting set passing: the panel shows it, with no notice.
    - Task checks failing at open: no hold; a fix draft, as in 00040.

## Decided for you

12. A trigger names the task checks `checks = "task-checks"`, as the shipped `startup-check` does;
    00040's `"gate"` is read as the same; no set may be named `task-checks`.
13. A set runs in a read-only snapshot of the user's branch's latest commit, shown in the
    verification panel under its name, traced with `orbit.verify.set`.
14. The fix is a coder task at the front of the queue titled `Fix the failing set: <set>`, listing
    the commits since the set last passed and its output, its origin the trigger that ran the set.
15. Holds are saved per repository beside the review count (each holding set, the commit it failed
    on, the fixes tried); frontends get additive `merge_holds` (sets and why) and
    `waiting_to_merge` (task ids) in session state, not a task status, so older frontends still
    read it; `ReleaseHold` is a new command.
16. A queued commit is reviewed when it lands, as `task_landed` fires on landing.
17. Notices: `Merges held: stress failed on 1a2b3c4. A fix is queued first.` · `Merges resumed:
    stress passed.` · `Merges resumed by hand; stress hasn't passed.` · `stress still fails after 3
    fixes; merges stay held. Release with ctrl+f or fix it yourself.`
18. The checks sections of `python`, `cpp` and `code-style` become **Suggested checks** tables
    (check, what it catches, tool, command, where it fits: task checks, or a set with its cadence
    and whether it holds merges). The configurator's default skills become `python` and `cpp`, so
    it has suggestions before a project maps any; this changes 00035's "configurator: none".
19. A proposal's steps gain their set, and proposals gain the sets' `holds_merges` and the
    triggers to add, as additive protocol fields; accepting writes `set`,
    `[verification.sets.*]` and `[triggers.*]` into `.orbit/config.toml`; the proposal view shows
    each step's set and when it runs.
20. `cpp/scripts/check_includes.py` uses only Python's standard library (`tomllib`), `cpp`'s
    `build.md` describes `include-rules.toml`, and the configurator copies the script to
    `tools/check_includes.py` and proposes the step `python3 tools/check_includes.py`.

## How it will be built

1. Write this record.
2. Put verification steps in sets: `VerificationStep.set`, `[verification.sets.*]` with
   `holds_merges`, validation, `checks = "<set>"` and `task-checks`, the rename, the reference
   config and `config show`.
3. Run named sets from triggers: on any event, in a snapshot, shown in the panel, re-runs folded,
   none under `--no-verify`, the reporting notice, spans.
4. Hold merges while a holding set fails: `MergeHolds` saved per repository, merges queued, the
   fix first and landing with what's beneath it, re-run and lift, the fallback limit, restore,
   notices, session state.
5. Show and release merge holds: the task list's title and rows, ctrl+f, `ReleaseHold`.
6. Suggest checks in the skills: the Suggested checks tables; the configurator's default skills.
7. Let the configurator propose sets and triggers: proposal fields, instructions, writing them,
   the proposal view.
8. Ship the C++ include checker: the script, `build.md`'s format, `cpp`'s suggestion.
