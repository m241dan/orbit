# Generic commands requirements

The decisions and plan for phase 1a of milestone 1 (generic protocol) of the control-plane design: the commands that name built-in roles become generic, and lane and task-origin names become strings.

## Goal

`orbit.protocol` names configured things, not built-in roles, so later milestones can add agents and triggers without growing the protocol. This phase replaces the role-specific commands with generic ones and loosens the lane and origin types, with no change a user can see.

## Scope

1. Milestone 1 ships as four separately planned phases: 1a generic commands with string lane and origin names (this record); 1b the envelope with command outcomes, `seq` and coded errors; 1c Hello/Welcome, Catalog, Snapshot and `since`; 1d the generated JSON Schema, its drift test and `docs/protocol.md`. Over one plan: a long chain where an early wrong guess spreads; over two phases: each still too large.
2. Settled now for later phases, not built here:
   - `Hello`/`Welcome` and `since` land in milestone 1, with the handshake in-process too. Over waiting for the server milestone: the protocol would change again then.
   - `Session` carries `Envelope[Command]` and `Envelope[Event]`. Over the envelope existing only in the JSON encoding: outcomes couldn't be matched to commands.
   - A frontend starting gets `Welcome`, `Catalog`, `Snapshot`, then live events, and every session open or new sends a fresh Snapshot. Over keeping today's piecemeal announcements: switching would differ from joining.
   - The envelope's `workspace` is the fixed `"default"` until the workspaces milestone. Over the root commit hash: fails outside git; over a path hash: changes when the repository moves.

## Behaviour

3. `FireTrigger(trigger, input)` replaces `RequestSuperReview` and `ConfigureChecks`, under the names `super-review` and `configure-checks`; each behaves exactly as the command it replaces.
4. `input: dict[str, str] = {}` holds the trigger's template values; both built-ins take none and refuse any keys given. Over free text: the triggers milestone's templates need named values; over leaving the field out: the command would differ from the design.
5. `SendMessage(agent, text)` replaces `SubmitPrompt`; only `planner` accepts it, and it behaves as `SubmitPrompt` does today.
6. `ResetConversation(agent)` replaces `ResetPlanner`, with only `planner` accepted; it is answered by `ConversationReset(agent)`, which replaces `PlannerReset`. Over keeping `PlannerReset`: a generic command would answer with a planner-only event.
7. `BusyLane` becomes `str` (values `work` and `planner`) on `CancelTurn` and `BusyChanged`.

## Edge cases and failure

8. Trigger, agent and lane names are `str` in the protocol; the backend refuses an unknown or unsuitable name with `ErrorOccurred` naming the valid choices, and phase 1b gives refusals codes. Over a fixed list of allowed names: a bad message would fail to parse and couldn't be answered, and later milestones would have to loosen the list; over ignoring it: the user wouldn't see why nothing happened.

## Data

9. `TaskOrigin` becomes `str`, keeping today's values `manual`, `planner`, `review` and `super_review`, so saved sessions load unchanged. Over renaming with a load mapping: it might be written twice if the triggers milestone picks other names; over renaming without one: older sessions would stop loading.

## Wording

10. The fixed names are constants in `orbit.protocol` beside the role names: `SUPER_REVIEW = "super-review"`, `CONFIGURE_CHECKS = "configure-checks"`, `WORK_LANE = "work"`, `PLANNER_LANE = "planner"`; the TUI and the backend both use them. Over backend-only constants: the strings would be written twice; over a labelled `BUILT_IN_TRIGGERS` now: it anticipates phase 1c.

## Interactions and done

11. Keys stay as they are (ctrl+g, ctrl+e, ctrl+n, the prompt) and send the new commands. No user-visible change, so README.md is unchanged. Done means the checks pass and the TUI behaves exactly as before.

## How it will be built

1. Write this record.
2. Make lane and task-origin names strings: `BusyLane` and `TaskOrigin` become `str` aliases, `WORK_LANE` and `PLANNER_LANE` join `orbit.protocol`, and `CancelTurn` with an unknown lane is refused.
3. Replace `RequestSuperReview` and `ConfigureChecks` with `FireTrigger`, adding `SUPER_REVIEW` and `CONFIGURE_CHECKS`; unknown triggers and any input are refused.
4. Replace `SubmitPrompt` with `SendMessage`; agents other than `planner` are refused.
5. Replace `ResetPlanner` and `PlannerReset` with `ResetConversation` and `ConversationReset`; agents other than `planner` are refused.

## Out of scope

The envelope, command outcomes, `seq`, coded errors, Hello/Welcome, Catalog, Snapshot, `since`, the JSON Schema and `docs/protocol.md` (phases 1b–1d); configured roles and triggers (later milestones).
