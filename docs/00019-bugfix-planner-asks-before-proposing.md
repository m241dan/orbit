# Planner asks before proposing requirements

The decisions and plan for fixing how the planner role settles details: choices it made itself while writing task prompts and only mentioned after proposing, a task whose Keep forbade what its Done required, and a task too big to finish.

## Goal

Every choice in a proposed task was put to the user before it was proposed, no prompt contradicts itself, and no task is so large it can't land. The fix is in the planner's instructions (`src/orbit/agent/external/instructions/planner.md`), so it holds in every session.

## Cause

The planner's rounds settled requirements at the level of the goal, and it read the code each task touches closely only when writing that task's prompt. Concrete cases surfaced then (a waiting session's state, a command's preconditions, where a number shown comes from), and it decided them in the drafts and told the user afterwards. Separately, task 244's Keep forbade what its Done required, so its coder gave up; and task 342 changed about 55 files at once and failed.

## Behaviour

1. Before posting the requirements, the planner reads the code each likely task touches and walks each task as its coder would; any behaviour a prompt would have to state that no answer settles is asked in a last round.
2. A requirement is marked as following only when it follows from an answer, an earlier record or the code; a choice of the planner's own is a question.
3. A prompt carries only confirmed decisions and facts from the code; a choice found while writing one goes back to the user before anything is proposed, never settled in a draft and mentioned afterwards.
4. The planner checks that no prompt's Keep forbids what its Done requires.
5. A task that would change most of a layer's files at once is split into steps that each leave the checks passing.

Over fixing only 1–3: the same session showed the other two failures, and they cost a given-up task and a failed one.

## How it will be built

1. Write this record.
2. Add the five rules to `src/orbit/agent/external/instructions/planner.md`, with a test that each is present in its section. (Landed before this record.)
