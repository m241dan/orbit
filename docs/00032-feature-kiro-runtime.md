# Kiro runtime requirements

The decisions and plan for milestone 9 (Kiro runtime) of the control-plane design: `kiro-cli` becomes orbit's second agent runtime behind the runtime port of `docs/00031-feature-runtime-port.md`, from facts captured on kiro-cli 2.32.0.

## Goal

A role can run on Kiro as it runs on Claude: with orbit's tools only through orbit's MCP server, its tool access enforced by Kiro's own tool list, its conversation resumable, its cost visible in credits, and its limits and outages pausing work instead of failing it.

## Running Kiro

1. `KiroCliRunner` in `orbit.agent.external.kiro_cli` implements `AgentLauncher`. Each agent runs `kiro-cli chat --output-format stream-json --agent orbit-<agent id> --agent-engine v2 --require-mcp-startup` in its workspace directory, with `--model`, `--effort` and `--resume-id <handle>` when they apply, and a first message telling it to fetch its task, as Claude's does.
2. The engine is pinned to V2 (`--agent-engine v2`). Over Kiro's default: a future V3 default would break configs silently; over V3 now: nothing captured on it.
3. Each agent's config is written to `~/.kiro/agents/orbit-<agent id>.json` just before it starts and deleted when it ends, also when cancelled or crashed; at startup orbit removes leftover `orbit-*` configs older than a day. Over the working directory's `.kiro/`: untracked files in worktrees and the checkout; over a private `HOME`: Kiro's login missing.
4. The config holds `prompt` (orbit's agent rules, then the role's instructions, as Claude gets them), `mcpServers` with only orbit's (by URL, with `Authorization: Bearer <agent token>`), `includeMcpJson: false`, and the tools for the role's `tool_access`, all pre-trusted: `off` orbit's tools only; `restricted` those plus Kiro's read-only built-ins (exact V2 names confirmed from Kiro's docs, listed in README); `full` `@builtin`. Over instructions in the first message: resuming replays them; over both: duplicated context; over leaving out `off`: `off` roles couldn't run; over `full` only: Kiro's restrictions unused.

## Capabilities and failures

5. Kiro declares `resume`, `usage`, every `tool_access` level and every effort level; `max_turns` false. When a run's `metadata` reports reasoning unavailable while an effort was set, a Notice says so once per role per session (until orbit restarts or the session is switched). Over no effort levels: blocks effort where models support it; over no Notice: effort silently ignored; over once per task run: repeats all day; over every agent: noisy.
6. `failure()` gives `api_unavailable` for a `runError` naming a connection failure (the captured wording), an HTTP 5xx or overload; `usage_limit` for one mentioning `limit`, `quota` or `credit` (case-insensitive), with no reset time; a Kiro usage-limit pause retries hourly (Kiro's limits reset monthly). Any other `runError` fails the task with Kiro's full message, and README asks for unrecognised wordings to be reported. Over captured wordings only: the first limit burns the queue; over every error as a limit: broken prompts pause forever.
7. `runFinished`: stop reason `end_turn` → `completed`, `cancelled` → `cancelled`, `max_tokens` or `max_turn_requests` → `max_steps`, anything else → `completed` with `orbit.agent.stop_reason` on the agent-process span. `runError` → `ErrorOccurred(agent_failed)` with the message, then `TurnFinished(error)`. Exit code 3 (orbit's MCP server unreachable) is a crash, "Kiro couldn't connect to orbit's MCP server at <url>", never a pause; exiting with neither event is a crash. Over strict stop reasons: new harmless ones fail tasks; over unknown as `max_steps`: wrong handling; over no `--require-mcp-startup`: a wasted turn and credits.

## Translation and usage

8. Consecutive `agent_message_chunk` pieces, and consecutive `agent_thought_chunk` pieces, are each joined into one `AssistantMessage`, emitted when another event arrives; `tool_call` → `ToolCallStarted` (with Kiro's tool name), `tool_call_update` with `completed` → `ToolCallFinished` with its output. The `sessionId` is the resume handle. Over dropping thoughts: reasoning lost; over each chunk as a message: fragments.
9. `Usage` gains `credits: float` (additive). A Kiro turn's usage is the sum of its `meteringUsage` credits, with zero tokens and dollars. Totals, spans (`orbit.cost.credits`) and metrics (`orbit.credits`) keep credits apart from dollars. Over usage unknown: Kiro's cost invisible; over converting at a rate: a rate to maintain.
10. The TUI shows what each figure has: dollars only (`$1.20`), credits only (`0.62 credits`), or both (`$1.20 · 0.62 credits`) when both are non-zero, for task rows, session totals and the usage panel alike. Over dollars with credits only when no dollars: Kiro's cost vanishes from mixed totals; over separate columns: more space everywhere.

## Models

11. `[runtimes.kiro.models]` ships `auto`, `sonnet = "claude-sonnet-4.5"`, `haiku = "claude-haiku-4.5"`; no `opus` or `fable`. Kiro's full-id rule: lowercase letters, digits, dots and dashes. A role with no model uses `auto`. The executable check looks for `kiro-cli`; `kiro` joins `RUNTIMES`. Over mapping `opus` to Sonnet: a quietly weaker model; over no aliases: names differing between runtimes for no reason.

## Fixtures and docs

12. The first coding task is `scripts/capture_kiro_fixtures.py`, a developer tool never run by the checks: it starts orbit's real MCP host on localhost with a test token, writes a temporary agent config, runs `kiro-cli` to call `get_task`, `report_progress` and `complete_task`, and forces an outage with an unreachable proxy, saving `tests/fixtures/kiro/*.jsonl` with home paths and session ids replaced. The coder runs it once and commits the fixtures; every translation test uses them. Over capturing by hand: orbit's own server and token unproven.
13. README covers using Kiro: installing and logging in, the engine pin, tool-access mapping, credits, aliases, effort per model, the monthly limit's hourly retry, and reporting unrecognised errors; the schema is regenerated for `Usage.credits`.

## How it will be built

1. Write this record.
2. Capture Kiro's output as fixtures: `scripts/capture_kiro_fixtures.py`, `tests/fixtures/kiro/`.
3. Count credits in usage: `Usage.credits`.
4. Write and remove Kiro agent configs: `kiro_agent_config`, `KiroAgentFiles`.
5. Translate Kiro's stream into orbit's events: `translate_kiro`.
6. Run agents on Kiro: `KiroCliRunner`, `kiro` in `RUNTIMES`.
