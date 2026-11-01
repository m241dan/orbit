# Lens reviews and schedules requirements

The decisions and plan for milestones 12d and 12e of `scratch/control-plane-design.md`, after
`docs/00041-feature-check-sets-and-merge-holds.md`: one reviewer per review lens reporting
findings, a merge pass filing tasks from them, narrow reviews of fixes, blocking findings holding
merges, reporting sets feeding lenses, schedules, and tabs grouped by trigger. The design is in
`scratch/skills-design.md` (*Reviews by lens*, *Check tiers*, *Stopping merges*).

## Goal

Each per-commit lens (Fit, Bugs, Tests, Logic, Security, Performance) and each super-review lens
(Style, Drift, Scaling) is its own agent, which reports findings rather than filing tasks. The
merge pass, the `reviewer`, starts once a review's lens agents have all ended. It checks and
groups their findings and files tasks under one cap. Fixes get a narrow review. Blocking findings
hold merges until a review shows them fixed. Reporting sets reach a lens through a trigger, and
triggers can fire on a cron schedule.

## Lens reviews

1. Each per-commit lens is its own role and trigger, reporting findings rather than filing tasks;
   over one reviewer with every lens (findings reworded across lenses, one cap per lens).
2. One commit's lens reviews run side by side in the review lane, up to `review_slots` (default
   3); over one at a time (reviews fall behind busy coders) and a lane per lens (large bursts, and
   it breaks the shared review lane).
3. The super review is three lens agents (Style, Drift, Scaling) whose findings go through the
   merge pass; over one super-reviewer with three lenses, and over filing directly (super reviews
   filed 89 of the 94 repeated findings).
4. The merge pass (the `reviewer` role) starts from a new event, `reviews_finished`, which fires
   once every lens task queued for one review has ended; over a guard on `task_finished` (hidden
   state) and lane code (not configurable).
5. Triggers gain `paths` (globs); Security runs on every commit until a project narrows it; over
   shipped path guesses (silently skipped reviews) and no filter.
6. The merge pass must decide every finding: filed, merged into another, or dismissed with a
   reason. `complete_task` is refused until it has, and findings left undecided when it gives up
   or is cancelled are recorded `unanswered`; over recording `unanswered` silently (noisier
   measurement) and counting them as dismissed (blurs a decision with an oversight).
7. Findings show in the lens and merge-pass transcripts, and their outcomes go on spans; over a
   findings panel (new UI and protocol) and spans only.
8. No shipped single-reviewer mode. A project that wants one disables the lens triggers and adds
   its own role and trigger, so `review-rounds` guards any number of triggers, and a role holding
   `suggest_task` still files tasks into its review's batch itself; over a disabled shipped
   trigger and a mode setting.

## Fixes and holds

9. Fix batches get a narrow review: the lenses that raised the batch's findings (super lenses
   included, reading the diff) plus Bugs and Tests, then the merge pass. Rounds are counted
   across lenses; over leaving super-review fixes to Bugs and Tests only, or unreviewed.
10. A blocking finding (Bugs or Security) that the merge pass files as blocking holds merges.
    Its fix goes first, in a batch of its own, and lands despite the hold. The hold lifts once
    the raising lens's narrow review of the fix reports no blocking finding; over lifting when
    the fix lands (a fix that didn't work would release merges), and no holds.

## Reporting sets and schedules

11. A new event, `set_finished`, with fields `set` and `status` and variables `{set}`,
    `{status}`, `{commit}` and `{output}`, lets a trigger queue a lens task carrying a set's
    output. The configurator proposes such triggers; over `reports_to` on sets (a second way to
    start agents) and shipped triggers for magic set names.
12. `schedule` triggers take `cron`, parsed by the new dependency `cronsim` in local time; over a
    hand-written parser (daylight saving bugs) and `croniter` (shaky maintenance).
13. A schedule missed while the workspace was closed fires once on open; over skipping missed
    times (a laptop closed nightly never runs its nightly set) and firing once per missed time
    (runs pile up).

## Tabs

14. Triggers gain `tab`. The per-commit lens triggers, `merge-pass` and the fix reviews share
    `review`, and the super review uses `super-review`. Each entry is labelled with its role;
    over a pinned tab per role (ten more tabs) and one fixed Reviews tab.

## Edge cases

15. 1. A commit of a user or planner task lands: each per-commit lens's trigger queues its task
       (Security too, unless its `paths` exclude the commit), up to `review_slots` running at
       once in the review lane.
    2. Every lens of a review reports nothing: no merge pass runs, and nothing is filed.
    3. A lens task fails, gives up or is cancelled: it counts as ended. The merge pass runs on
       the other lenses' findings once all have ended.
    4. A lens reports past `lens_findings` (10): the extra findings are refused, and the
       refusal says the cap is reached.
    5. A lens role is disabled: its triggers never fire, and `reviews_finished` waits only for
       the tasks queued.
    6. The merge pass has more findings than its cap (`review_suggestions`, or
       `super_review_suggestions` for a super review): it files the most severe and dismisses
       the rest with the reason "over the cap".
    7. The merge pass groups several findings into one task: the first is recorded `filed`, the
       others `merged` into it, on the merge pass's span.
    8. A filed task lands, or gives up because the work is already done: its findings are
       recorded `fixed` or `given up` on that task's span.
    9. A batch of fixes finishes: narrow review by the lenses that raised its findings (super
       lenses included, reading the diff), plus Bugs and Tests, then the merge pass.
    10. Narrow reviews reach `review_rounds`: review pauses with today's notice, and resumes
        when one of the user's tasks lands.
    11. The merge pass files a blocking finding: merges hold, with `Merges held: a blocking
        finding from bugs-reviewer in 1a2b3c4: <title>. Its fix is queued first.` The fix goes
        first, in a batch of its own, and lands despite the hold.
    12. The raising lens's narrow review of that fix reports no blocking finding: `Merges
        resumed: the finding from bugs-reviewer is fixed.` If it reports a blocking finding
        again, the hold stays and another fix is queued, up to `fallback_fixes`, then the
        step-in notice (ctrl+f releases).
    13. That narrow review can't run (rounds limit reached, or the lens disabled): the hold
        stays, with a notice asking the user to step in.
    14. The merge pass dismisses a blocking finding, or files it as normal: no hold.
    15. A finding hold while a set holds, or two finding holds: merges wait until all lift.
    16. Orbit restarts during a finding hold: the hold is restored, saved per repository as set
        holds are.
    17. The super review comes due, or ctrl+g: the one `super-review` trigger, with one count,
        queues the Style, Drift and Scaling lens tasks, then the merge pass, all in the
        `super-review` tab.
    18. Any named set run ends: `set_finished` fires, `passed` or `failed`, but not for the
        task checks. A failed reporting set with no trigger listening still gives today's
        notice; with a trigger listening there's no notice, and the lens task carries the
        output.
    19. A `schedule` trigger comes due while its lane is busy, or while its set runs: its task
        waits on the board, or the set run folds into one more run, as today. Under
        `--no-verify` sets don't run.
    20. `cron` is invalid, is set on another event, or is missing on `schedule`: a
        configuration error at load, naming the trigger.
    21. A schedule was missed while the workspace was closed: it fires once on open, after the
        `session_started` triggers. Last-fired times are saved per repository.
    22. Two workspaces are open: each runs its own schedules.
    23. Review tasks were waiting at upgrade: they're cancelled, with a notice naming the
        commits left unreviewed (their prompts are for the old reviewer).
    24. A project file overrides `roles.super-reviewer`: a configuration error at load,
        `super-reviewer is now style-reviewer, drift-reviewer and scaling-reviewer.`
    25. A trigger's `paths` match no file in the commit, or in any of a batch's commits: that
        trigger doesn't fire for it.
    26. A running agent's trigger has no `tab`: it gets its own tab, as today; with a `tab`, its
        agents share that tab, each entry labelled with its role.

## Decided for the user

16. Roles: `fit-reviewer`, `bugs-reviewer`, `tests-reviewer`, `logic-reviewer`,
    `security-reviewer`, `performance-reviewer`, `style-reviewer`, `drift-reviewer` and
    `scaling-reviewer`, all in the review lane, sharing one instructions file,
    `lens-reviewer.md`, each with its lens skill. `reviewer.md` becomes the merge pass's
    conduct; `super-reviewer` and its markdown go; over one markdown per lens (nine copies of
    one conduct).
17. A lens reports with a new tool, `report_finding(title, body, severity)`, severity
    `blocking` or `normal`. Only roles with `blocking_findings = true` (shipped on Bugs and
    Security) may report `blocking`. A lens may report up to `lens_findings` (10) per review.
18. The merge pass's `suggest_task` gains `findings` (ids, at least one) and `severity`, and a
    new tool, `dismiss_finding(finding, reason)`, records dismissals. Finding ids are `F1`,
    `F2`, … within a review.
19. A *review* is the finding-role tasks (roles granted `report_finding`) one firing queued: a
    landing, a batch review, a super review or a set report. `reviews_finished` fires when they
    have all ended. Its fields are `findings` (`some`/`none`) and `review` (`commit`, `batch`,
    `super`, `set`); its variables are `{trigger}`, `{title}`, `{commit}`, `{findings}`,
    `{cap}`, `{queued}` and `{landed_since}`. The shipped `merge-pass` trigger takes
    `when = { findings = "some" }`. Its cap is `review_suggestions`, or
    `super_review_suggestions` for a super review.
20. Narrow reviews need no new event: a batch review is still heard by the triggers
    `review-rounds` guards, and `task_landed` gains the field `review` (`commit` or `batch`). A
    new guard, `raised-finding`, holds a trigger back on a batch review unless its role raised
    one of the batch's findings, and passes on commit reviews. The shipped `super-fix-review`
    trigger takes `when = { review = "batch" }`.
21. A trigger's `role` may be a list, queuing one task per role from one firing.
    `super-review` keeps its name, its one count, ctrl+g and its sample, and lists the three
    super lenses.
22. Findings are kept in the review lane's saved state until their batch is reviewed: role,
    title, body, severity, outcome, the task filed and the reason. This bumps the session
    format version, with its migration, fixture and regenerated schema. Waiting review tasks
    saved under an older version are cancelled (edge case 23).
23. Spans: a lens's review span carries `orbit.lens` and `orbit.review.findings`; the merge
    pass's span `orbit.findings.filed`, `.merged`, `.dismissed` and `.unanswered`; a fix task's
    span `orbit.findings.fixed` and `.given_up`.
24. A finding hold is a merge hold: `MergeHold` gains an additive `finding` (its title), with
    `set` naming the raising lens role. The task list's title reads `merges held: blocking
    finding`. Notices as in edge cases 11 and 12; the step-in notice and ctrl+f as for sets.
25. `paths` is valid only on `task_landed` (elsewhere a configuration error). It matches git
    paths with `fnmatch`, `**` crossing directories, against the commit, or every commit of a
    batch.
26. `tab` reaches frontends as an additive `tab` on `RunningAgent`, `AgentStarted` and
    `TriggerEntry`. The strip pins a tab for each `tab` of the enabled triggers, after the
    built-in roles' tabs, and a role whose agents start only from tabbed triggers has no pinned
    tab of its own.
27. `schedule` has no `when` fields and gives `{trigger}`. Last-fired times are saved per
    repository beside the review count. A trigger that has never fired doesn't catch up.
    Schedules run only while their workspace is open.
28. A set report goes through the same review and merge pass, its batch at round 1. The
    configurator may propose a `set_finished` role trigger with an inline prompt, an additive
    proposal field. The `python` and `cpp` Suggested checks name the lens each reporting set
    feeds: mutation testing `tests-reviewer`, benchmarks `scaling-reviewer`.
29. Settings `review_slots = 3` and `lens_findings = 10` go beside `review_rounds`, in the
    reference config and `orbit config show`. The README, `docs/telemetry.md` and
    `docs/protocol.md` change in the task whose behaviour they describe.

## How it will be built

1. Write this record.
2. Collect findings and fan in with `reviews_finished`: `report_finding`, `Finding`, reviews as
   groups of finding-role tasks, `lens_findings`, the role field `blocking_findings`,
   `review-rounds` guarding many triggers, the event with its fields and variables, the format
   bump.
3. Have the merge pass decide every finding: `suggest_task`'s `findings` and `severity`,
   `dismiss_finding`, the `complete_task` refusal, `unanswered`, the outcome spans.
4. Ship the per-commit lens reviewers and the merge pass: the six roles, `lens-reviewer.md`,
   the `merge-pass` trigger and `merge-pass.md`, `reviewer.md` as the merge pass, the `review`
   trigger replaced, waiting reviews from older versions cancelled.
5. Run lens reviews side by side: `review_slots` in the review lane.
6. Review fix batches narrowly: the `review` field on `task_landed`, the `raised-finding`
   guard, the shipped fix-review triggers.
7. Split the super review into lenses: `role` as a list, the three super lens roles,
   `super-review` and `super-fix-review`, the `super-reviewer` error.
8. Group tabs by trigger: the trigger field `tab`, the additive protocol `tab`, the strip.
9. Hold merges on blocking findings: finding holds, `MergeHold.finding`, the fix's own batch,
   lifting by narrow review, notices, restore.
10. Filter triggers by paths: `paths` on `task_landed`.
11. Raise `set_finished` for named sets: the event, the notice only when no trigger listens,
    set reports through the merge pass.
12. Let the configurator route reporting sets to lenses: the proposal field, its instructions,
    the Suggested checks.
13. Fire triggers on a schedule: `schedule`, `cron` with `cronsim`, last-fired times, catching
    up once on open.

## Deferred

- Moving a lens between per-commit and the super review from its measured dismissals: the spans
  give the data; moving is configuration.
- A findings panel in the TUI.
