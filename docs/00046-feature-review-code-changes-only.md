# Review code changes only requirements

This records what was agreed for holding back the code-focused lens reviews of a landing that changes no code, such as a requirements record or a README edit.

## Goal

Every lens reviews every landed commit today, so a commit that only writes a record in `docs/` gets six reviews. A trigger's `paths` can already narrow a `task_landed` trigger by glob, but only per project and only by hand. Orbit should, by default, hold back the code-focused lens reviews of a landing that changes no code, and say so.

## Behaviour

1. **Built in:** by default, the lens reviews skip a landing that changes no code. (Over configuring `paths` in Orbit's own `.orbit/config.toml` only: only this repository benefits, and every new project must remember it. Over an opt-in: off unless someone turns it on.)
2. **What counts as code:** a changed file under a `[[skills]]` entry's path, or with an extension in `CODE_EXTENSIONS` (`orbit.agent.domain.review_sample`), or named as a dependency or build manifest (edge case 14), matched by file name at any depth. A project without `[[skills]]` entries uses the other two. (Over extensions alone: `pyproject.toml` and Orbit's shipped Markdown skills and instructions would go unreviewed. Over mapped paths alone: manifests at the root would be missed.)
3. **Which reviews skip:** `bugs-review`, `tests-review`, `logic-review`, `security-review` and `performance-review`; `fit-review` always runs. (Over all six: a docs-only task gets no check that it did what it was asked. Over only bugs, tests, logic and performance: three reviews rather than one of a non-code change.)
4. **`super-fix-review` is exempt.** (Over the same rule: the style lens judges docs and comments, so a docs-only fix of a style finding is what it should see again.)
5. **Mixed commits** are reviewed as today, the whole diff included. (Over cutting non-code files from the diff: reviewers lose context, such as a README that should have changed.)
6. **A note in the conversation** says what was skipped: `Skipped the bugs, tests, logic, security and performance reviews of "<title>": it changed no code.` (Over silence: the user would wonder where the reviews went.)
7. **How it's expressed:** a trigger guard, `code-changed`, on the shipped `bugs-review`, `tests-review`, `logic-review`, `security-review` and `performance-review` triggers. A project drops it by setting that trigger's `guards` without it, or adds it to its own `task_landed` triggers. A trigger that sets its own `paths` is decided by `paths` alone. (Over a project-wide `review_code_only` limit: it can't differ by lens, and it's a second mechanism beside guards.)

## Edge cases

| # | Given … | → outcome |
|---|---|---|
| 1 | The landing changes only `docs/`, `README.md` or `.orbit/` | The five guarded lenses don't queue; fit runs; the note in 6. |
| 2 | It changes anything under `src/` or `tests/` in Orbit, `.md` included | All six run, as today. |
| 3 | It changes only `pyproject.toml` or `uv.lock` | All six run: they're manifests. |
| 4 | It changes a `.py` file outside the mapped paths, such as `scripts/x.py` | All six run: its extension makes it code. |
| 5 | The project has no `[[skills]]` entries | Code is a code extension or a manifest. |
| 6 | A batch review of several commits | Decided on all their changed files together: any code means all run. The note names the first task, then `and N more`. |
| 7 | A commit deleting or renaming a code file | Counts as a code change. |
| 8 | A trigger that sets its own `paths` | Decided by `paths` alone; `code-changed` is ignored for it. |
| 9 | Git can't list the changed files | The reviews fire, as with `paths` today, the failure noted on the span. |
| 10 | Some guarded lenses are disabled, or held back by another guard | The note names only the lenses `code-changed` held back; no note if it held none. |
| 11 | Every lens is held back (fit disabled, a docs-only landing) | Nothing queues and there's no merge pass, as when `paths` holds them all today; the note still shows. |
| 12 | A trigger on another event than `task_landed` names `code-changed` | Refused as the configuration loads: `triggers.<name>.guards: code-changed needs on = "task_landed".` |
| 13 | A project drops `code-changed` from a lens trigger's `guards` | That lens reviews every landing, as today. |
| 14 | The manifests | `pyproject.toml`, `uv.lock`, `poetry.lock`, `Pipfile`, `Pipfile.lock`, `setup.cfg`, `requirements*.txt`, `package.json`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.toml`, `Cargo.lock`, `go.mod`, `go.sum`, `CMakeLists.txt`, `CMakePresets.json`, `vcpkg.json`, `conanfile.txt`, `meson.build`, `Makefile`, `pom.xml`, `build.gradle`, `build.gradle.kts`, `Gemfile`, `Gemfile.lock`, `Dockerfile`, `Containerfile`. |

## Decided for you

8. **Where the rule lives:** `changes_code(changed, mapped_paths) -> bool` and `MANIFEST_NAMES` in `src/orbit/agent/domain/code_changes.py`, reusing `CODE_EXTENSIONS` and the under-a-path test of `orbit.agent.domain.review_sample`, made public there as `is_code_file` and `is_under`. `CODE_EXTENSIONS` stays where the `orbit` skill points. (Over moving it: that pointer would go stale.)
9. **The lenses in the note:** each held-back trigger's roles, a role named without its `-reviewer` suffix (`bugs`), else whole, joined as `a, b and c`. The title is the task's first line, as task rows show it.
10. **No note on quiet replays:** the reviews raised again as a session loads (`reviewed_quietly`, `fan_in_quietly`) post no note. (Over noting them: each restart would repeat old notes.)
11. **The reference config** is regenerated, with `code-changed` in the guards' description and on the five triggers.

## How it will be built

1. Write this record.
2. Add the `code-changed` trigger guard: `GuardName` gains `code-changed` and `check_triggers` refuses it off `task_landed`, in `orbit.agent.domain.triggers`; `orbit.agent.domain.code_changes` with `changes_code` and `MANIFEST_NAMES`; `Triggers._within_paths` in `orbit.agent.application.triggers` holds guarded triggers back when nothing changed is code and publishes the note as a `Notice`; the README's Triggers section.
3. Guard the five code lenses with `code-changed`: `src/orbit/agent/external/triggers/triggers.toml`; the regenerated `reference/config.toml`; the README's Reviews section.

## Out of scope

- Cutting non-code files from the diff a reviewer reads.
- Changing when the super review falls due.
