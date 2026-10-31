---
name: review-logic
description: Review lens for one commit — is each piece of the change the right code type (Domain, Application, External, Execution, Entry), keeping that type's rules, with new dependencies placed by the two-question test? Use when reviewing where code and dependencies belong.
---

# Logic lens

**Your question:** is each piece of this change the right code type, keeping that type's rules?

## Look for

| Type | Holds when | What to look for |
|---|---|---|
| Domain | Its results depend only on its inputs | Reading the clock or environment, async code, I/O in disguise |
| Application | It sequences work, asks Domain for decisions, reaches outside through ports, uses Execution for concurrency | A business rule written inline in a workflow; building a concrete adapter itself |
| External | One thin adapter per outside system, turning library data and errors into the project's types | Business rules in an adapter; library types leaking past it |
| Execution | It knows how work runs and nothing about what the work is | Business names or rules in a scheduler, supervisor or helper |
| Entry | It builds, configures and starts, and reads like a list | Parsing, branching on config, start and stop logic |

For each new dependency: is it placed by the two questions in `code-style`'s `dependencies.md`,
and did the change add its rule to the import contracts?

Across types:

- Decisions and user-facing wording belong in Domain, as plain functions.
- State is held by its one owner and changed through it, with the next state computed in Domain;
  a second copy of a fact kept in step by hand is a finding.
- A contract two sides share is written once, in the module both import, and each side's
  collaborators come in at construction.
- Execution steps such as cancel-and-wait and shielded finishes go through the Execution helper,
  rather than being written inline again.

## Measure against

`code-style`'s `types.md` and `dependencies.md`, the project's map of where each type lives (its
`layers` skill), and the kind skill.

## Leave to the other lenses

Whether the code is correct: Bugs. Names, docs and consistency: Style, in the super review.
Patterns that build up across commits: Drift, in the super review. This lens looks at this
commit.

## A finding

Name the piece (file and line), the type it is and the type it sits in, the rule it breaks, and
where it belongs. Logic findings never block merges.

Most changes keep their types. When this one does, report no findings.
