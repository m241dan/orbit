# Lanes from roles requirements

The decisions and plan for phase 7b of milestone 7 (roles as data) of the control-plane design: lanes, workspaces, conversations and effort are driven by the role fields phase 7a added, so a project's own roles behave as their configuration says.

## Goal

A role's configuration decides where its agent works, which lane runs it and when, whether its work lands, whether you can talk to it, and how hard it thinks; and the TUI lets you queue any role's task and talk to any conversational role.

## Record and cap

1. The cap counts tasks whose role has `workspace = "worktree"`, keeping phase 4c's coder-and-configurator rule as data; `docs/00027-feature-roles-as-data.md` decision 3 is updated accordingly. Over `merge = true`: configurator runs escape the cap; over a `capped` field: one more field.

## Lanes

2. One generic runner per distinct lane among enabled task roles; each takes the board's next runnable task whose role is in its lane, one at a time. Workspace, verification, merging, fixes and the cap come from the task's role fields. Today's review queuing rules stay with the reviewer and super-reviewer tasks `ReviewLane` queues until milestone 11. Over keeping the work and review lanes plus a generic one: three kinds of lane; over moving the review rules to guards now: pulls milestone 11 forward.
3. A lane whose roles merge runs only on ctrl+r (`RunTasks`), held by ctrl+s, paused at usage limits and outages; every other lane starts by itself when a task for it is queued. A usage or outage pause stops every lane. Over everything on ctrl+r: reviews would stop starting by themselves; over everything automatic: coder tasks would start on acceptance.
4. Workspaces: `worktree` the coder's tree; `snapshot` a fresh read-only snapshot of the base; `checkout` the user's checkout; `none` an empty temporary directory created per run and removed after. The Snapshot's `busy` holds one flag per lane; `CancelTurn(lane)` accepts any lane.
5. New validation, config errors naming the roles: `verify = true` needs `merge = true`; every enabled role with `workspace = "worktree"` must share one lane. Over verified work on a side branch: a branch policy nobody asked for; over leaving it to be put aside: checked work vanishes; over a lock across lanes: they wouldn't run beside each other; over a worktree per lane: deferred parallel coders.

## Enabled

6. `enabled = false` removes a role: a disabled reviewer or super-reviewer queues no reviews of that kind, and a disabled super-reviewer takes `super-review` (ctrl+g) out of the Catalog; a disabled configurator takes `configure-checks` (ctrl+e) out; `SendMessage` to a disabled planner is refused with `not_configured`; a disabled coder's tasks are refused with `not_configured` when added or run, and no startup fix drafts are added. The Catalog's agents leave it out. Over coder-only refusal at load: a special case; over ignoring tasks: they'd sit forever.

## Conversations

7. One seat per enabled conversational role, each with its own conversation, lane and settings. The session record's `planner_conversation` and `settings` become per conversational role (`conversations: {role: id}`, `role_settings: {role: Settings}`); older records load as the planner's. `SendMessage`, `ResetConversation` and `CancelTurn` already take names. Over one conversational role: half the gap left open; over a shared seat: switching loses an in-flight turn.
8. The prompt talks to the conversational role whose tab is shown, or the planner on any other tab; its placeholder names who. ctrl+t cycles the conversational roles' tabs (footer "Conversations"); ctrl+x and ctrl+n act on the one shown, or the planner elsewhere. Over a target key: more state; over `@mentions`: parsing and typos.

## TUI

9. The task editor's **Role** field, between Prompt and Model, lists the enabled task roles with their lanes and defaults to `coder`; choosing a role fills Model, Tools and Effort with its defaults unless changed in this edit; a pending task's role can change, a running one's can't. Over not filling: easy wrong tool access; over title prefixes: an invisible convention.
10. The task editor's **Effort** field (unset, `low`, `medium`, `high`, `xhigh`, `max`); **ctrl+y** opens an effort picker (footer "Effort"); the session's model, tools and effort pickers set the shown conversational role's settings. Over shared session settings: one role's `max` applies to all; over the editor only: no planner effort.
11. Effort is passed to agents as `--effort`, with `CLAUDE_CODE_EFFORT_LEVEL` removed from their environment; unset passes nothing.
12. Task rows whose lane isn't `work` show the lane as a dimmed tag; **L** in list mode cycles a lane filter (all, then each lane), the list's title showing it ("Task list · review"). Over a tag only: no narrowing; over a list per lane: the single list split again.

## Docs

13. README's roles section drops the "take effect later" marks and covers lanes, workspaces, `enabled`, conversational roles, the Role and Effort fields, ctrl+y, ctrl+t's new meaning and **L**; the schema is regenerated if the Snapshot's `busy` or any message changes.

## How it will be built

1. Write this record and update `docs/00027-feature-roles-as-data.md` decision 3.
2. Tighten role validation and count the cap by worktree.
3. Run every lane from its roles: `LaneRunner`.
4. Run each task in its role's workspace: `workspace_for`.
5. Honour `enabled = false`.
6. Give each conversational role its own seat: `ConversationSeats`.
7. Pass effort to agents.
8. Add Role and Effort to the task editor.
9. Talk to any conversational role from the prompt.
10. Show lanes on the task list.
