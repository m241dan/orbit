# Review loop repeats requirements

The decisions and plan for stopping reviews from suggesting work that already landed or is already queued, and for keeping super reviews from running while their earlier suggestions are still pending.

## Goal

A review never queues a task whose title an earlier suggestion of the session already had, or that a task on the board already has, whatever its status; super reviews are paced by the user's own commits and wait while review-suggested work is pending; and only work that merged into the base is reviewed or counted.

## What was observed

In the "Orbit the Control Plane" session, 811 tasks: 464 were review suggestions, 242 reviewer runs. All 21 give-ups said the work was already done. Of 62 pending suggestions on 2026-10-23, 12 had landed already and 6 duplicated another pending one; three super reviews proposed the same extraction. The causes are in `domain/reviews.py` (`refusal` matches only pending or running tasks, `count` counts every landing) and `application/landing.py` (`land` returns a commit whenever the head moved).

## Decisions

1. **The duplicate guard checks every task on the board, drafts included, by its normalised first line.** Over the pending-or-running rule: a suggestion that matches a completed, gave-up, unverified or removed task is the repeat the loop keeps producing.
2. **The planner remembers the normalised title of every suggestion it accepted, per session, and refuses a title it remembers.** The memory is saved in `PlannerSnapshot` and restored with it. Over checking the board alone: a suggested task that was removed, or that finished long ago, leaves no trace on the board. Over fuzzy matching: exact after normalisation is predictable and cheap; a reworded repeat is accepted and documented as the limit.
3. **A reviewer's prompt lists the first lines of the tasks pending or running on the board**, up to a fixed number, so it can avoid them before calling `suggest_task`. Over relying on refusals alone: a refused suggestion still costs the reviewer's turn and one of its cap.
4. **Only commits from the user's and the planner's tasks count toward `super_review_every`.** Over counting every landing: review-generated commits made super reviews fall due three or four times a day and re-review their own output.
5. **A super review that falls due waits while any task with origin `review` or `super_review` is pending or running**, as it already waits while review is paused, and fires on the first qualifying landing after. Over firing at once: it would re-find the work those tasks are about to do.
6. **`ctrl+g` (the manual trigger) is unchanged**: it reviews now and restarts the count.
7. **A commit counts as landed only when its merge result is `merged`.** A task whose merge ended `conflict`, `blocked`, `detached`, or whose base kept moving, stays `completed` with its detail and its `TaskMerged` event, but is reported to the review lane as ended without landing, and doesn't count toward a super review. Over the head-moved rule: work that isn't on the base can't be reviewed against it.

## How it will be built

1. `reviews.py`: the guard over every board task and the suggestion memory in `PlannerSnapshot`; `review_prompt` takes the pending titles; `review_lane.py` passes them. Tests in `test_reviews.py`, `test_review_lane.py`, `test_review_lane_snapshots.py`; README's Reviews paragraph.
2. `reviews.py`: `count` takes the landed task's state and the board's tasks; `review_lane.landed` passes them. Tests; README and `config.py` docstrings.
3. `landing.py`: `Landing` carries the merge result; `work_lane._land` reports to the review lane only for `merged`. Tests in `test_landing.py` and `test_work_lane_state.py`; README.
