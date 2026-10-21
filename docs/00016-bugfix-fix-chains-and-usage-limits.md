# Fix chains and usage limits requirements

This record holds what was agreed for fixing a fix task that completes the task it fixes after that task's work was put aside, for pausing a run at Claude's usage limit and starting it again once the limit resets, and for serve tests that hang instead of failing; and how the fixes are built.

## Goal

On 2026-10-20 task 342 (step 4 of `docs/00015-feature-many-workspaces.md`) failed Ruff lint and fell back: ❗ `unverified`, its work left uncommitted in the coder's tree, fix task 453 queued after it. The fix hit Claude's session limit and ended in error, and the run moved on: every task after it hit the same limit at once (440–446, 450 and 452 in that log, about 70 tasks over the night), and the first of them, not being a fix, put the tree's changes aside, stashing 342's work. After the reset, 453 ran on a clean tree, found nothing to fix, changed nothing and was skipped as `no_changes`, so it completed, and `Board.mark_fixed` turned 342 ✅ although none of its work had landed. Steps 5 to 7 then gave up on a protocol still at version 1. Recovering it later, `tests/test_serve.py` hung rather than failed: its frontend stops `orbit serve` only when its work succeeds.

After the fix, a task whose verification failed is only ever completed by checks that pass on a tree holding its work, a usage limit pauses the run and it starts again by itself once the limit resets, and a failing serve test fails.

## Behaviour

1. A fix task is always verified when it claims done, even if it changed nothing (over skipping it as `no_changes` like any task: a fix that changes nothing completes the task it fixes with no check having passed on that task's work). Tools `off` still skips: such a task can't change files.
2. A fix task whose coder's tree holds no work, no changes and no commits its base branch lacks, gives up before an agent starts: `Gave up: The work it fixes isn't in the coder's tree any more; look for it in git stash list.`, the task it fixes staying ❗ (over running it: its checks pass on the base and complete the task it fixes; over ending it in error: an error runs again on every `ctrl+r`, giving up the same way each time). Without a tree there's nothing to tell, so the fix runs as before.
3. A fix task that ends in error stops the run there, with a notice saying why: the work it fixes stays in the tree, and the next run starts with the fix (over moving on: the next task that isn't a fix puts that work aside; over restoring the put-aside work when the fix runs again: a stash to track per chain, and conflicts once the base moves). A cancelled task stops the run already; a fix that gives up or fails its checks leaves the chain as today.
4. A task that ends in error because its agent hit a usage limit stops the run (over moving on: every later task fails the same way at once, and the ones that aren't fixes put a fix's work aside). The agent's error says so in words, such as `You've hit your session limit · resets 6pm (UTC)` or `Claude AI usage limit reached`; that's matched in the domain, case-insensitively, as `hit your … limit` or `usage limit reached` (over a status field: `claude -p` gives none).
5. The run then starts again by itself, at the reset time the usage monitor has: the latest `resets_at` among the limits used up (100%), if it's still ahead, plus a minute's grace; otherwise, with usage off, unavailable or not caught up yet, 15 minutes later, when a limit still reached pauses it again at once (over reading the time in the error's words: a time of day in a named zone, with no date; over waiting for `ctrl+r`: the queue would sit idle all night). A notice says when, as `Claude's usage limit is reached, so the run stopped. It starts again by itself at Wed 21 Oct 18:01 UTC; ctrl+r starts it now and ctrl+s cancels that.`, and another when it starts again.
6. The task that hit the limit is in error, as today, so the run starting again runs it first, afresh: what its agent changed is put aside like any rerun's (over carrying on from its partial work: a new agent finding changes it didn't make). A fix carries on from the tree, as fixes do.
7. While a run waits to start again, `ctrl+r` starts it now, `ctrl+s` cancels the wait with a notice (`The run won't start again by itself.`), and a review's work doesn't start it early. Switching sessions drops the wait, as it belongs to the session left; so does closing orbit. A wait ending while the session is halted starts nothing, as for a review's work.
8. In `tests/test_serve.py`, the frontend's thread stops `orbit serve` however its work ends, so a failure on that thread fails the test (over a timeout on `runner.invoke`: a slow machine would fail it wrongly).

## Edge cases

- A fix task can hit the usage limit itself: the run pauses for the limit (5), and starting again runs the fix first, on the tree that still holds its work.
- The latest usage can be older than the limit, as the monitor fetches at most once a minute: the 15-minute retry covers that, and the usage is fresh by then.
- A task whose agent hit the limit after `get_task` may have changed files; that work is stashed by the rerun, never deleted (6).

## How it will be built

1. Add this record.
2. Always verify a fix task: `skip_reason` (`src/orbit/agent/domain/verification.py`) takes whether the task is a fix, which `Verifier.check` passes.
3. Give up a fix whose work is gone: `CoderTree.holds_work`, in `GitTree` (`git status --porcelain`, and `HEAD` not an ancestor of the base) and the test fake, checked by `WorkLane._run_agent` before the agent starts.
4. Stop a run at a fix's error or a usage limit: `WorkLane.run` returns why the run ended; the limit rules (`limit_hit`, `resume_time`, the notes) live in `src/orbit/agent/domain/usage_limits.py`.
5. Start a paused run again: `WorkTurns` waits until the resume time, timed by `Agents.pause` (clock, sleep, retry and grace), reading the latest usage the control plane hands each host (`WorkspaceHost.latest_usage`).
6. Stop `orbit serve` from the serve tests' frontend thread in a `finally`.
7. README: fallbacks, skipped verification and the run's usage-limit pause.

## Out of scope

- A time limit on verification steps or the coder role, and progress from `run_check`, which Claude Code aborts after 300 silent seconds; noted in `docs/bugs/`.
- The planner's turns at the usage limit: they fail as today.
- Restoring a stash of put-aside work automatically.
