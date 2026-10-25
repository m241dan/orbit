# Roles as data requirements

The decisions and plan for phase 7a of milestone 7 (roles as data) of the control-plane design, with the milestone-wide decisions it sets up: the built-in roles become data in the package, roles gain the fields later phases act on, and instructions can come from files.

## Goal

Every role, built-in or custom, is described by the same data: what it's told, where it works, which lane runs it, whether its work lands, whether you can talk to it, and how hard it thinks. Phase 7a loads and validates that data with behaviour unchanged; later phases act on it.

## Milestone 7

1. Milestone 7 ships as four phases: 7a roles as data, behaviour unchanged (this record); 7b every agent run with a task on the board, lanes driven by role fields, `enabled`, a Role field in the task editor, the prompt talking to any conversational role, effort passed to agents with its editor field and picker; 7c the user layer and `orbit config show`; 7d the reference `config.toml`, `orbit init`, and the configurator and orbit's own file using it. Over five phases: the board change wouldn't be usable alone; over three: unrelated pieces in one plan.
2. Every agent run with a task is a board task: coder and configurator tasks as today, plus reviews and super reviews queued by orbit in the `review` lane, marked with role and lane (the task list shows a lane tag and filter). Planner turns stay conversation. The review rules (rounds, batches, cap) move onto the board's review tasks unchanged. Over board tasks only for merging lanes: two ways to queue work for triggers; over planner turns as tasks: a chat message as a task is clumsy.
3. `lane` is any name; each distinct lane runs one agent at a time, lanes run beside each other; the built-ins use `work`, `review` and `planner`. The coder cap counts tasks whose role has `merge = true`. Over a fixed set: custom roles can't run beside others; over the cap counting the lane named `work`: a merging role elsewhere escapes it.
4. `workspace`: `worktree` the coder's separate worktree, which can merge; `snapshot` a read-only snapshot of the base commit; `checkout` the user's checkout, read in place; `none` an empty temporary directory. Over no `checkout` guard: a role could edit your tree; over dropping `checkout`: the planner couldn't see uncommitted work.
5. Milestone 11's triggers build on 7b's board. The events explored for them, each raised at one code point: `plan_proposed` (a planner turn ends having proposed drafts), `drafts_accepted`, `run_started`, `run_finished`, `run_stopped` (with a reason; usage and outage pauses start no agents), `task_finished` (with outcome), `task_gave_up`, `task_unverified`, `task_landed`, `commits` with `every`, `branch_moved` (the base moved outside orbit), `batch_landed`, `startup_check_failed`, `session_started`, `manual`, `schedule`. Today's reviews, super reviews, configurator and startup fix become shipped triggers; guards (`review-rounds`, `not-queued`, `dedupe`) keep their rules in the domain.

## Data

6. The built-in roles ship as `roles.toml` beside their Markdown in the package's role directory (today's `instructions/`), holding every field `_BUILT_IN_FIELDS` holds today plus the new ones and reproducing today's roles exactly; `_BUILT_IN_FIELDS` is removed.
7. `Role` gains `lane: str` (a plain name), `workspace` (`worktree`/`snapshot`/`checkout`/`none`), `conversation: bool`, `merge: bool`, `enabled: bool`, and `effort` (`low`/`medium`/`high`/`xhigh`/`max`, or unset). The built-ins: coder `work`/`worktree`/merge; configurator `work`/`worktree`/no merge; reviewer and super reviewer `review`/`snapshot`; planner `planner`/`checkout`/conversation; all enabled; no effort set.
8. A custom role leaving fields out gets the coder's defaults: `lane = "work"`, `workspace = "worktree"`, `merge = true`, `verify = true`, `conversation = false`, `enabled = true`, and today's defaults for the rest. Over safe defaults: existing custom roles stop landing work; over required fields: existing configs break.

## Validation

9. In the domain, each a config error naming the role and the rule: `merge = true` needs `workspace = "worktree"` and `verify = true`; `workspace = "checkout"` can't have `tool_access = "full"`; `conversation = true` needs `propose_task` in its tools and not `get_task`, `merge = false`, `verify = false`, a workspace other than `worktree`, and a lane no task role shares. Over looser conversation rules: collisions in the worktree; over the planner only: no other conversational roles.
10. `instructions`: a value with no newline ending in `.md` is a path, relative to `.orbit/` for project and user roles and to the package's role directory for built-ins; anything else is the text itself; a missing file is a config error naming it. Over two keys: two keys for one thing; over paths only: one-line roles need files.
11. A built-in's instructions change only through config (`[roles.coder] instructions = "roles/coder.md"`); an unreferenced `.orbit/roles/<name>.md` does nothing. Over convention: a stray file silently changes an agent; over both: two ways to one effect.

## Effort

12. `effort` is also part of `Settings` (an additive protocol field, schema regenerated), so tasks and the planner's session can carry it. In 7a it's loaded and stored only; 7b passes it as `--effort`, removes `CLAUDE_CODE_EFFORT_LEVEL` from agents' environments, and adds the task editor's field and the planner's picker. Values follow `claude --help` (`ultracode` isn't offered to `-p`). Over a role field only: no per-task effort; over waiting for the runtime port: wanted now.

## Docs

13. README's roles section documents the new fields in 7a, marking `lane`, `workspace`, `conversation`, `merge`, `enabled` and `effort` "accepted now; take effect in a later version" until 7b lands. Over documenting them in 7b: keys accepted meanwhile that the README doesn't mention.

## How it will be built

1. Write this record.
2. Ship the built-in roles as data: `roles.toml`, `read_built_in_roles`.
3. Read role instructions from files: `resolve_instructions` for project roles.
4. Add the new role fields: `lane`, `workspace` (`Workspace`), `conversation`, `merge`, `enabled`, with validation.
5. Add effort to roles and settings: `Effort`, `Role.effort`, `Settings.effort`.
