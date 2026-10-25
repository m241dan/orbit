# Runtime port requirements

The decisions and plan for milestone 8 (runtime port) of the control-plane design: the agent runtime becomes a choice per role, each launcher declares what it can do and recognises its own failures, models resolve through per-runtime alias tables, conversations remember their runtime, and usage pauses belong to the runtime that hit them.

## Goal

orbit no longer assumes Claude: a role names its runtime, orbit refuses configurations a runtime can't honour before anything runs, and one runtime's limits never stop another's work. Only `claude` exists in this milestone; later runtimes plug into the same port.

## The port

1. `Capabilities` (frozen) declares `resume`, `usage`, `max_turns`, `tool_access: frozenset[ToolAccess]` and `effort: frozenset[Effort]`; every launcher exposes its own; Claude declares all, with every level. Over the design's four only: a runtime without effort would be passed a flag it rejects; over `mcp` and `web`: milestone 12 decides those.
2. `conversation()` and `set_conversation()` take and return an opaque handle only the launcher understands; the port's docstrings stop speaking of Claude; `ResumeFailed` means "this launcher couldn't carry that handle on".
3. The launcher classifies its own failures: `failure(message) -> Failure | None` with the kind (`usage_limit` or `api_unavailable`) and a reset time when the error states one; Claude's wraps `limit_hit` and `api_unavailable`. A runtime may offer a usage source, as `Capabilities.usage` says (Claude's is today's monitor). Over a global Claude-worded pause: other runtimes' limits unrecognised; over each runtime's pause stopping every lane: one runtime's limit idles the others.

## Choosing a runtime

4. `runtime` on roles takes effect: per role in the project; per role or for all roles in the user layer (a machine-bound field beside `model` and `effort`); `claude` from the package; phase 7c's precedence. Only `claude` is known: any other value is a config error, "… this version of orbit knows the runtimes claude." `--runtime <name>` on `orbit chat` and `orbit serve` sets, for that run, the runtime of every role that doesn't name its own. Over `--agent-cli`: two names for one thing; over overriding every role: a project's per-role choice broken.
5. A role asking for what its runtime can't do is a config error at load naming the role, runtime, request and what's supported: a `tool_access` level, an `effort` value, or `conversation = true` without `resume`. A runtime without `usage` is allowed; its agents' usage shows as "unknown". Over warning and ignoring: weaker restrictions than declared; over warnings for effort: just as surprising.
6. `orbit.cli` builds only the runtimes some enabled role uses (after the layers and `--runtime`); a used runtime whose executable isn't on PATH stops startup with exit 2 naming the roles, the runtime and what to install. Over failing at the first agent: a whole run's first task fails; over warning: a half-working setup.

## Models

7. Each runtime has an alias table: Claude's ships in the package (`[runtimes.claude.models]` passing `fable`, `opus`, `sonnet`, `haiku` through); the user layer's `[runtimes.<name>.models]` adds or overrides entries. A model in its runtime's table resolves through it; anything else is a full id only if the runtime says it looks like one (Claude: starting with `claude-`), else a config error at load listing the aliases. A task's model set in the TUI that doesn't resolve is refused with `invalid` when added or edited. Over passing anything through: typos fail at run time; over aliases only: new models need a config change.
8. The TUI's model pickers list the role's runtime's aliases with each resolved id muted; `SettingsChanged` carries the shown role's choices with their ids (additive); `MODEL_ALIASES` gives way to the tables.

## Saved conversations

9. Sessions save each conversation as `{runtime, handle}`; older records' bare ids load as `claude`. When a conversational role's runtime differs from its saved conversation's, the session opens normally but messages to that role are refused with `invalid` ("The architect's saved conversation is from claude, and it now runs on kiro; reset it (ctrl+n on its tab) to start one on kiro."); `ResetConversation` clears the handle; other roles are unaffected. Over starting fresh with a Notice: asking was chosen; over bare handles: a handle could be misread.

## Pauses per runtime

10. A pause belongs to the runtime that hit it and restarts at the runtime's stated reset time, else its usage source's, else `docs/00016-bugfix-fix-chains-and-usage-limits.md`'s 15-minute retry or `docs/00023-bugfix-pause-runs-when-claude-is-down.md`'s backoff. A lane whose roles merge keeps its order and waits when its next task's runtime is paused; other lanes skip to their next task on a runtime that isn't paused. `SessionSummary` and `SessionStateChanged` gain `paused_runtime`; the TUI says "run paused: usage limit on claude…". `UsageChanged` gains `runtime` (additive). Over every lane skipping: a dependent task could land out of order; over every lane waiting: one runtime idles all reading work.

## Visibility and docs

11. The Catalog's `AgentEntry` gains `runtime`; `orbit config show` shows each role's runtime with its layer; telemetry's `orbit.runtime` comes from the role's runtime.
12. The reference config's `runtime` key moves from the later-version section into the roles' fields and the loader stops refusing it; `[runtimes]` belongs to the user config and isn't in the project reference. README covers runtimes, `--runtime`, capabilities, alias tables, per-runtime pauses and the conversation reset; the schema is regenerated.

## How it will be built

1. Write this record.
2. Make the launcher port runtime-neutral: `Capabilities`, `Failure`, `AgentLauncher.capabilities`, `AgentLauncher.failure`.
3. Choose each role's runtime: `RUNTIMES`, `build_runtimes`, `--runtime`.
4. Check roles against their runtime's capabilities.
5. Resolve models through each runtime's alias table: `ModelAliases`, `resolve_model`.
6. Save conversations with their runtime.
7. Pause each runtime on its own.
