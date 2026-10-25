# Consultants requirements

The decisions and plan for milestone 10 (experimenter) of the control-plane design, redesigned: instead of a task the planner blocks on, the experimenter is a conversational role the planner consults, through a data-driven `consult` mechanism any conversational role can be given; replies arrive later as messages; findings can be kept as reports; and every window shows what it cost.

## Goal

The planner delegates what it can only learn by running code to a consultant working in a throwaway copy, keeps planning meanwhile, and picks the answer up when it arrives; the user can watch the consultant work in its own tab, talk to it directly, keep its findings, and see what every agent and window cost.

## Consulting

1. A role field `consults = ["<role>", …]` gives that role the orbit tool `consult(role, message)`. `consults` is allowed only on conversational roles (task roles read saved reports instead), may name only enabled conversational roles other than the role itself, and a cycle is a config error naming the roles. Phase 7a's conversational rule is relaxed: a conversational role has no `get_task` and doesn't merge; its other tools are its own. Over cycles: loops burn the limit; over a planner-only tool: data was chosen; over task roles consulting: the task ends before a reply, or blocks holding a coder slot.
2. `consult` returns at once, with the consultation's id. Requests to a consulted role queue in order with the user's own messages to it, one at a time in its lane; the asking role's transcript shows "→ Asked experimenter: <first line>", with "(2nd in line)" while queued. Over refusing when busy: the asker retries by itself; over a capped queue: another number.
3. The reply is the consulted turn's last assistant message (its instructions say to end with a complete answer), delivered to the asking role as a message marked from the consultant: at once if that role is idle, after its current turn otherwise, after the user's waiting message if any, several together in one turn. Over the whole turn joined: the asker reads the working every time; over a `reply` tool: one more thing to remember.
4. In the asking role's tab, a delivered reply is one collapsed entry: "↩ Experimenter's reply to '<first line>' · N lines" and its first three lines, enter opening it in the detail pane. Over full text with rules: floods; over a pointer only: the tab no longer tells the story.
5. The asking role always gets a reply: a cancelled (ctrl+x, restart, switch), failed (error, limit, outage), denied or expired consultation arrives as "The experimenter's consultation '<first line>' was interrupted: <why>." Over dropping it: the asker might wait; over retrying once: a meant cancel runs again.
6. A role field `consult_limit_per_hour` (default 6 for every role; 0 means no limit) counts the asking role's consultations in a rolling hour. Beyond it the request is held and every frontend viewing the session gets a modal ("The planner wants to ask the experimenter: '<first line>'. It has asked 6 times this hour.") with "Allow", "Allow for the rest of the hour" (that role consults freely for an hour from the approval) and "Deny"; the first answer from any frontend applies and closes the others'. Held requests expire as denied after an hour with no frontend connected. Over waiting indefinitely: a backlog floods the user on return; over no limit: nothing stops a loop; over a default of 0 for custom roles: easy to forget.
7. `consultation_log(consultation)`, an orbit tool for roles with `consults`, returns the consulted turn's full working: every assistant message, thinking included, and each tool call's name with the first 2,000 characters of its output, as text.

## The experimenter

8. A built-in conversational role `experimenter`, with its own tab and lane: full tool access; `save_report`, `list_reports`, `read_report`; no `consults`; workspace `copy`; `model = "sonnet"`, no effort, `timeout_minutes = 30`, network allowed. Its instructions: run what's asked in the copy, change nothing else, end with a complete answer, save a report when a finding will matter later. Settable like any role. Over the planner's model: often Opus; over Haiku: weak at debugging.
9. Workspace `copy`: in a git repository, a writable copy of the base commit with the project's setup run in it, under `.git/orbit/`; outside git, a copy of the workspace directory (without `.orbit/`) in orbit's state directory, with setup run. Created at a conversation's first turn and recorded in the session; it survives turns, switches and restarts; removed on reset (ctrl+n) or after its role's `copy_ttl_hours` (default 24) since the conversation's last turn, checked at startup and hourly. Never the user's checkout; never merged. Over a fresh copy each turn: follow-ups lose their setup; over removal on switch or stop: a restart loses it; over a fixed TTL: long investigations can't keep it; over refusing outside git: no experiments in plain directories.

## The planner

10. The built-in planner gains `consults = ["experimenter"]` and the three report tools. Its instructions' rule "ask the user to run it and paste the output" becomes: consult a role it may consult, saying in the same message what and why, and carry on meanwhile; ask the user only for facts about their own machine or accounts beyond the copy; save a report when a finding will matter beyond the conversation. Over asking before each consultation: against the discretion wanted; over cost-based asking: complicated while defaults are small.

## Reports

11. Reports are stored per workspace in orbit's store (id, title, Markdown body, author role, session, time). `save_report(title, body)`, `list_reports()`, `read_report(id)` are ordinary orbit tools granted through `tools`: the experimenter and planner get all three; coders and reviewers get `list_reports` and `read_report`. **ctrl+k** opens the report list in the TUI (enter shows one, `y` copies it). Putting a report into the repository is a task the planner proposes. Over files in the checkout: writes outside the task flow; over planner write-ups only: unproposed findings lost; over everyone saving: clutter.

## Usage

12. Every window shows its usage: each conversation tab its role's total; each task agent's tab its agent's total; each agents-pane row its running agent's cost so far; the task list's corner the session total. Totals per role are saved with the session (`role_spent`). Over conversation tabs only: task agents' tabs without totals; over no agents pane: running cost is where it matters.
13. A consultation's cost counts for the consultant and is shown as caused by the asking role: a conversation tab's corner reads `planner $0.42 · via experimenter $1.10, architect $0.30 · $1.82` with up to two consultants, `via 3 consultants $1.70` beyond; **u** on a conversation tab opens a breakdown per role (spent itself, spent for whom). The session total counts each cost once; credits follow `docs/00032-feature-kiro-runtime.md`'s rule. Over always grouping: one consultant unnamed; over naming all: overflow.

## Telemetry and docs

14. A consulted turn's span `orbit.consultation` (asking role, consulted role, queue wait, outcome) starts its own trace, linked to the asking turn's span.
15. The schema and `docs/protocol.md` cover the consultation events, the approval request and decision, reports and `role_spent`; README covers consultants, `consults`, the experimenter, `copy` and its TTL, the hourly limit and approval, reports and ctrl+k, usage per window and **u**.

## How it will be built

1. Write this record.
2. Add consultation fields to roles: `consults`, `consult_limit_per_hour`, `copy_ttl_hours`.
3. Add the `copy` workspace: `CoderTree.copy`.
4. Ask and answer consultations: `consult`, `ConsultationAsked`, `ConsultationAnswered`, the `orbit.consultation` span.
5. Answer interrupted consultations.
6. Limit consultations per hour, with approval: `ApprovalRequested`, `DecideApproval`.
7. Let roles read a consultation's working: `consultation_log`.
8. Store and browse reports: the report tools, `ReportsScreen`.
9. Add the experimenter and let the planner consult it.
10. Show consultations in the transcript.
11. Track usage per role and who caused it: `role_spent`, `caused_spent`.
12. Show usage in every window.

## Deferred

- The planner choosing a consultant's model and effort per consultation, with guidelines in its instructions.
- Agents discovering each other through A2A-style agent cards (the Agent2Agent protocol, now under the Linux Foundation: each agent publishes a card at `/.well-known/agent-card.json` listing its skills, capabilities, authentication and endpoint), instead of fixed `consults` lists.
- Several consultations of one role at once, run in parallel instead of one queue, so a role can start as many consultants as a job needs.
