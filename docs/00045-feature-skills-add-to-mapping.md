# Skills add to the mapping requirements

This records what was agreed for how an agent's skills combine — its role's, its task's and the project's `[[skills]]` entries' — and for showing each task's role and the skills its agent gets on the task list.

## Goal

Today a task that names skills gets them in place of the `[[skills]]` entries', so a task naming `python` in Orbit loses `layers` and `orbit`, and so do the reviews of its commit, which inherit its skills. The task list shows only a task's own skills and its lane, so six lens reviews of one commit all read `review` and `skills: python`, though each reviewer also gets `code-style` and its own lens. Task skills should add to the mapping, and each row should say which role runs it and every skill its agent gets.

## Behaviour

1. **Task skills add to the path mapping:** an agent gets its role's skills, then its task's, then every `[[skills]]` entry's, each once, where first named. (Over replacing, as today: a task naming `python` loses `layers` and `orbit`. Over adding with a per-task opt-out: a new field in the task editor and the tool for a rare need.)
2. **Mapped skills' "applies under …" lines** come in an agent's instructions whenever the entries give it skills, a task naming skills of its own included. (Follows from 1.)
3. **Rows carry their role:** the dim tag after a task's title reads `<role> · <lane>`, and just `<role>` for the `work` lane (`coder`). (Over the role in place of the lane: which lane it waits in is lost. Over the role's label: it differs from the names config files and `orbit config show` use.)
4. **Rows show the skills the agent gets**, as it's given them at launch: `skills: <role's> + <task's, then mapped>`; with no role skills, no `+` (`skills: python, layers`); with nothing delivered, no skills segment. (Over the task's own skills alone: misleading, as the commit reviews showed.)
5. **Drafts and pending rows show it too.** (Over showing it only once the task starts: a draft's skills can't be checked before accepting it.)

## Edge cases

| # | Given … | → outcome |
|---|---|---|
| 1 | The task names a skill the mapping also uses | Given once, where the task names it. |
| 2 | The task names no skills | Role + mapping, as today, with the "applies under …" lines. |
| 3 | The task names skills, and there's a mapping | Role + task + mapping, with the "applies under …" lines. |
| 4 | No `[[skills]]` entries | Role + task, as today. |
| 5 | A super review, which reviews no particular task | Role + mapping, as today. |
| 6 | Row, its role has no skills | No `+`: `skills: python, layers`. |
| 7 | Row, nothing delivered | No skills segment, as today. |
| 8 | Row whose role is no longer in the workspace's Catalog entry (removed or disabled since) | Tagged with the task's role name, with no lane; skills show the task's and the mapped ones, no role part. |
| 9 | Row of a running or finished task after the configuration changed | Shows what an agent started now would get, from the current Catalog. |
| 10 | A `work`-lane row | The tag is just the role, `coder`. |
| 11 | TUI joined to an older server that sends no role or mapped skills | Shows the task's own skills, as today. |
| 12 | `orbit config show --skills` | A skill the entries use is `mapped` for every role whose `skills` don't name it (no more `task / mapped`); `task` and `reviewed task` stay for skills only a task's list brings; `role` as today. |
| 13 | `read_skill` | Allowed for every skill delivered, mapped ones included, even when the task names skills. |

## Decided for you

6. **The combining rule is written once, in the protocol:** `given_skills(role_skills, task_skills, mapped_skills)` in `src/orbit/protocol/skills.py`, exported from `orbit.protocol`, used by the backend's `orbit.agent.domain.skills` and by the TUI. (Over a copy in the TUI: the two would drift.)
7. **Wire fields:** `AgentEntry.skills`, the role's skills, and `WorkspaceEntry.mapped_skills`, the `[[skills]]` entries' `use`, in order, each once; both default to empty, so older peers ignore them and a TUI joined to an older server shows the task's own skills. The schema is regenerated and `docs/protocol.md` updated; the protocol version isn't bumped, as the fields only add with defaults. (Over the Catalog's top-level `agents`: roles and mappings differ by workspace.)
8. **Shown skills follow the current configuration:** rows read the role's and mapped skills from the workspace's Catalog entry, not from a record of what was given at launch. (Over storing each task's delivered skills: a session format change for a display.)

## How it will be built

1. Write this record.
2. Make task skills add to the path mapping: `given_skills` in `orbit.protocol`; `given_skills`, `delivered_skills` and `skill_instructions` in `orbit.agent.domain.skills` follow it; the `--skills` map in `orbit.agent.domain.config_report` and `orbit.cli.config_text`; the README's Skills sections.
3. Tag task rows with their role: `task_label` in `orbit.tui.view.tasks` shows `<role> · <lane>`, or `<role>` for the work lane, in place of the lane tag; the README's Tasks section.
4. Send roles' and mapped skills in the catalog: `AgentEntry.skills` and `WorkspaceEntry.mapped_skills`, filled by `agent_entry` in `orbit.agent.domain.catalog` and by `CatalogEntries` in `orbit.agent.application.host_views`; the regenerated schema; `docs/protocol.md`.
5. Show the skills each task's agent gets on its row: `CatalogState.mapped_skills(workspace)` in `orbit.tui.view.catalog`; the task list takes each role's skills and the mapped skills from the router and renders them with `given_skills`; the README's Tasks section.

## Out of scope

- Changing which skills reviews, fixes and suggestions inherit from the tasks they come from.
- Recording the skills an agent was given at launch.
