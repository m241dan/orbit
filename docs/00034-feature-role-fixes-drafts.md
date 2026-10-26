# Role fixes drafts requirements

The decisions and plan for letting a conversational role, such as the planner, fix the drafts it
proposed until the user accepts them.

## Goal

A role that proposes tasks can edit, remove, move, list and read its own drafts until the user
accepts them, so it cleans up its own mistakes instead of asking the user to edit or drop drafts.

## Behaviour

1. A role holding `propose_task` can edit, remove, move, list and read its own drafts until the
   user accepts them; over replace only (it can't withdraw or reorder) and edit and remove only
   (a wrong order means proposing again).
2. The tools are `edit_draft(task_id, title, prompt)`, `remove_draft(task_id)`,
   `move_draft(task_id, before)`, `list_drafts()` and `read_draft(task_id)`; over one
   `change_draft(action, …)` tool, whose optional arguments are easy to misuse.
3. `move_draft` puts the draft right before `before`, another of the role's own drafts; without
   `before` it moves to the end of the list; over `before` being any task that hasn't started (it
   could jump ahead of the user's tasks) and a position number (it goes stale).
4. `list_drafts` returns `<id> · <title>` lines for the role's drafts, in list order;
   `read_draft` returns one draft's title and full prompt; over full prompts in the list (long
   replies).
5. An edited draft stays a draft, in its place, its title and prompt replaced as `propose_task`
   builds them; over the user's rule, where an edit makes the task pending.

## Interactions

6. Only drafts the calling role proposed can be touched: each task records `proposed_by`, the
   role that proposed it; drafts saved before this have none, so no role can touch them; over any
   role touching any draft (one role could rewrite another's).
7. The five tools come with `propose_task`; naming one in a role's `tools` is refused:
   "edit_draft comes with propose_task; list that instead."; over separate grants (a proposing
   role could be left unable to clean up) and ignoring the entry (config that has no effect).
8. The user's edit still makes a draft pending, so it's accepted and the role can no longer touch
   it; over the user's edit keeping it a draft (the role could overwrite the user's changes).
   Following from this: if the role edits a draft the user has open in the editor, saving the
   user's version overwrites the role's and accepts the task.

## Wording

9. An edited draft's row ends its dim details line with ` · edited by <role>`; a move doesn't set
   the mark, and accepting clears it; over a `✎` (it doesn't say who) and ` · revised` (it doesn't
   name the role).
10. Replies: "Edited draft task 12." · "Removed draft task 12." · "Moved draft task 12 before 9."
    / "Moved draft task 12 to the end." · `list_drafts` with none: "You have no drafts."
11. Refusals: a task that isn't a draft: "Task 12 isn't a draft any more; the user accepted or
    edited it." · another role's task, or one with no proposer, and likewise a `before` that isn't
    the caller's: "Task 12 isn't one of your drafts." · moving a draft before itself: "A draft
    can't move before itself." · an unknown id: the board's "No task with id '12'." · an empty
    title: "A task needs a title."

## Docs

12. The planner's instructions replace the paragraph beginning "Proposals are drafts the user
    accepts" with: "Proposals are drafts the user accepts, edits or removes on the task list.
    Until the user accepts one, you can fix it: `edit_draft`, `move_draft` and `remove_draft`
    change your own drafts, and `list_drafts` and `read_draft` show them as they stand. Once
    accepted or edited by the user, a draft is theirs; take the user's words as the truth about
    what it says. When you find a mistake in a draft, or the user changes their mind about one,
    fix the draft rather than asking the user to. For one the user has accepted, name it to
    remove and propose its replacement." README describes the tools, the mark and the rule for a
    role's `tools`.

## How it will be built

1. Write this record.
2. Record who proposed each draft: `TaskState.proposed_by`, set by
   `TaskBoard.add_draft(task, proposed_by)`.
3. Let a role list and read its drafts: `DRAFT_TOOLS` in `orbit.agent.domain.roles` (in
   `ORBIT_TOOLS`), `granted_tools(tools)` adding them to a role holding `propose_task`, the
   refusal of decision 7, and `list_drafts` and `read_draft` end to end over
   `TaskBoard.drafts_of(role)`.
4. Let a role edit and remove its drafts: `TaskBoard.edit_draft(task_id, task, role)`,
   `TaskBoard.remove_draft(task_id, role)`, `TaskState.edited` and the row's mark.
5. Let a role move its drafts: `TaskBoard.move_draft(task_id, before, role)`.
6. Teach the planner to fix its drafts: decision 12's passage in `planner.md`.
