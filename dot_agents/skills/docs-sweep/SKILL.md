---
name: docs-sweep
description: Sweep agent-docs, code comments, and user-facing documentation for drift against the code, and return a ledger of findings before changing anything. Use for a documentation rehash, doc drift audit, comment sweep, or stale-docs check across a repository.
user-invocable: true
---

# Docs Sweep

Documentation rots silently. A per-PR check keeps the docs a change touches honest; this skill is
the other half — a periodic, repository-wide sweep that finds the drift no single PR was ever
responsible for.

Read-only by default. Produce the ledger, present it, and change only the rows the user selects.
The one exception is a run the user explicitly opens with "fix all actionable rows".

Sweep the three surfaces **independently**. They rot for different reasons and a finding in one is
not evidence for another.

## Scope

Default scope is the whole repository. Narrow it when the user names a path, a subsystem, or a
time window (`git log --since`). State the scope you used in the first line of the report.

Before reading prose, build the ground truth to compare against:

- `git log --oneline -n 50` and `git diff --stat <base>..HEAD` for what has moved recently.
- The public surface: exported symbols, CLI subcommands and their `--help`, config keys, feature
  flags, environment variables.
- The build and test commands that actually work today, from CI config rather than from the README.

Never assert drift from prose alone. Every finding names the code that contradicts it.

## Surface 1 — `agent-docs/`

Plans, design rulings, and deep-dive evidence. These describe where the code is going, so they are
allowed to differ from the code — but only in the forward direction.

- **Overtaken plans.** A plan whose milestones have shipped, or whose approach was abandoned. It
  needs a dated addendum saying so, not a silent rewrite and not deletion.
- **Superseded rulings.** A ruling contradicted by a later ruling or by the code. Both documents
  stay; the earlier one gets the addendum pointing forward.
- **Stale pinned-source claims.** A ruling derived from a dependency at a specific revision is
  stale the day that pin moves. Check each such claim against the current pin.
- **Misplaced documents.** A plan or deep dive sitting at the repo root instead of `agent-docs/`.
- **Orphans.** A document nothing references and no current work depends on.

## Surface 2 — comments and doc comments

- **Restates the code.** A comment that says what the line already says is noise. The comments
  worth keeping record *why* a decision was made and what the alternative would have cost.
- **Falsified.** A comment describing behavior, ordering, ownership, or invariants the code no
  longer has. Highest severity on this surface: a wrong comment is worse than no comment.
- **History in comments.** Changelog entries, "previously we...", commented-out code. That is
  what `git` is for.
- **Generated verbosity.** Long obvious comments and doc blocks that restate signatures. Treat
  dense AI-style commentary as a smell and propose deletion, not rewording.
- **Wrong register.** Public API documentation written as an internal note, or an internal note
  promoted to public documentation.
- **Stale headers.** Copyright and license headers that drifted from the repository standard.

## Surface 3 — README and module documentation

Compare claim by claim against the implementation, not against filenames or commit subjects.

- **API descriptions** that no longer match signatures, defaults, or return shapes.
- **Examples and code blocks** that would not run. Run them where cheap; trace them where not.
- **File trees and layout diagrams** that list paths which no longer exist, or omit ones that do.
- **Build, test, and run commands** that no longer work — compare against CI.
- **Orphaned references** to symbols, modules, flags, or endpoints that were removed or renamed.
- **Undefined-before-use.** A symbol used in prose before the document defines it.
- **Missing documentation** for a public surface that has none, where its siblings all do.

## Ledger

Return this table before any edit:

| # | Surface | Where | Finding | Contradicting evidence | Class | Proposed change | Confidence |
|---|---|---|---|---|---|---|---|
| 1 | agent-docs / comment / docs | `path:line` | One sentence. | `path:line` of the code that disproves it, or the command whose output does. | `stale`, `contradicted`, `orphaned`, `restates-code`, `missing` | The smallest edit that makes it true, or `delete`. | high / medium / low |

Rules:

- **`Contradicting evidence` is required.** A row without a concrete `file:line` or command output
  is not a finding. Drop it rather than padding the ledger.
- `contradicted` outranks `stale`: text that is actively wrong is more urgent than text that is
  merely old.
- Group duplicates into one row and keep every location in the `Where` cell.
- Order by surface, then by severity within surface. Put `contradicted` rows first overall.
- Propose deletion freely. Documentation that carries no fact costs more than it returns.
- Do not propose rewrites for style, tone, or house voice. That is `simple-english`'s job, and only
  when the user asks.

## After the sweep

For selected rows only:

1. Apply the edits.
2. Re-read each modified file for internal consistency — fixing one claim often falsifies a
   neighbouring one.
3. In `agent-docs/`, add dated addenda; never silently rewrite a ruling out of existence.
4. Return an updated mini-ledger marking each row `fixed`, `deferred`, or `not actionable`.

Documentation-only changes belong in their own small PR, separate from the code they describe.
