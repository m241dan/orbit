# Review checks before trial requirements

This is the record of what was agreed for showing a configurator's proposed checks to the user before orbit runs any of them.

## Goal

Setting up orbit on an existing C++ repository with no checks, orbit tried a configurator's proposal, an allsan build among it, before the user saw it, and the build ran the machine out of memory. Orbit now shows every proposal to the user before anything in it runs, so they can have commands tuned (fewer build threads, say) first, and tells the configurator how big the machine is so its first proposal fits.

## Behaviour

1. The user sees every proposal before any of it runs. Accepting it starts the trial: the setup, then each step in order. If all pass, orbit saves the checks and the task completes, with no second window. If a step fails, the agent gets the step's name and output and revises, as before. Over showing it before and again after the trial: two windows per proposal. Over keeping the window after the trial and letting the configurator talk with the user first: nothing enforces that it asks.
2. The trial runs the setup and every step, those in sets included, as before. Over trying only the task checks: a broken set step would surface only when its trigger first fires. Over a per-step "skip trial" toggle in the window: more control, but the most UI.
3. The user tunes steps with `r`, asking for changes in words, as before; the revision is shown again before anything runs. Over editing commands in the window: needs a new editor UI and validation of what's typed.
4. Orbit tells the configurator the machine's CPU count and memory in its prompt, and its instructions and `cpp`'s Suggested checks tell it to set parallelism explicitly in every build and test step, such as `cmake --build … -j N` or `env CMAKE_BUILD_PARALLEL_LEVEL=N …` (steps run without a shell, so `env` sets a variable). Over instructions alone: at workspace `read` the agent can't measure the machine and would guess. Over neither: the user would spend revise rounds sizing commands.
5. This applies to every configurator proposal: the first setup, `ctrl+e` reconfigures, and every revision after a failed trial or a request for changes. Over only when no steps are configured: `ctrl+e` would keep the same risk.

## Edge cases

6. Given the user accepts and every step passes → orbit saves the checks and the task completes; the conversation notes `proposal accepted`, and the agent's reply line completes as before.
7. Given the user accepts and a step fails → the agent gets the step's name and output and revises, counting toward `fix_attempts`; its revised proposal opens a new window before anything runs.
8. Given the window after a failed trial → it shows the new proposal only; the failure is in the conversation already, on the reply line. Over saying it in the window: a protocol change for something already shown.
9. Given the user presses `ctrl+s` during the trial → the run stops, the running step's process group is killed, nothing is saved, and the task ends as `ctrl+s` ends any task.
10. Given the user answers `r` or `x` → nothing has run; `r` sends their words to the agent and the revision opens a new window; `x` ends the task 🛑, as before.
11. Given a proposal breaking a rule (a step name clash, a bad report) → refused to the agent before any window, as before.
12. Given revisions past `fix_attempts` → the task ends without accepted checks, as before.
13. Given the machine's memory can't be read (not Linux, say) → the prompt gives the CPU count alone; with neither, it says nothing of the machine.

## Wording

14. The window's last line `✅ All passed on the current code.` becomes `Nothing has run yet: accepting tries the setup and each step here, then saves them if all pass.`, and its key hint `a accept · r revise · x end` becomes `a accept and try · r revise · x end`.
15. The machine line, appended to the `{checks}` text a configurator's task is given: `This machine: 4 CPUs, 7.6 GiB of memory. Set each build and test step's parallelism to fit it.`; with memory unknown, `This machine: 4 CPUs. Set each build and test step's parallelism to fit it.`; with neither known, no line.

## Decided for the user

16. The machine facts are read once, when a workspace opens: a `Machine(cpus, memory)` value in `orbit.agent.domain.machine`, memory in bytes as the `Bytes` NewType, which `read_machine()` in `orbit.agent.external.machine` reads (the CPUs the process may use, from `os.sched_getaffinity`, else `os.cpu_count`; physical memory from `os.sysconf`), each `None` when it can't be read, and the CLI puts on a new `Agents.machine` field. Over reading it on every firing: the machine doesn't change while orbit runs.
17. The machine line is appended to the `{checks}` variable's text, from `describe_machine(machine)` in `orbit.agent.domain.machine`. Over a new `{machine}` template variable: a project that customised the `configure-checks` prompt wouldn't get it, and each event's variables would need it added.
18. No protocol or stored-format change: `ChecksProposed` keeps its fields; only the window's text changes.

## How it will be built

1. Write this record.
2. Show proposed checks before trying them: `ProposalTrial.decide` asks the user first and tries only checks they accept; the proposal window's last line and key hint change; the `Ask` and `ProposalReview.review` docstrings and README's "Setting up verification" say so.
3. Tell the configurator this machine's size: `Machine`, `Bytes` and `describe_machine` in `orbit.agent.domain.machine`, `read_machine` in `orbit.agent.external.machine`, the `Agents.machine` field the CLI fills, the line appended to `{checks}`, and the guidance in `configurator.md`, `cpp`'s `checks.md` and README's `{checks}` entry.

## Out of scope

- Editing proposed commands in the window.
- Stopping a trial when the agent exits during it; it behaves as before.
