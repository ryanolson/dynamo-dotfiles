---
name: docs-sweep
description: Sweep the book, code comments, and user-facing documentation for drift against the code, and return a ledger of findings before changing anything. Use for a documentation rehash, doc drift audit, comment sweep, or stale-docs check across a repository.
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

## Surface 1 — the book and `agent-docs/`

The book (`docs/src/`) describes the code as it is, so it must match the code exactly.

- **Contradicted chapters.** A chapter that describes behavior, numbers, or structure the code no
  longer has. Measured numbers need the command that produced them; a number without one is
  `missing` evidence.
- **Plan residue.** Phase or milestone labels, ruling numbers, PR numbers, ticket identifiers, or
  handoff notes in the book. History belongs in `git`, plans in Linear.
- **Stale pinned-source claims.** A claim derived from a dependency at a specific revision is
  stale the day that pin moves. Check each such claim against the current pin.
- **Orphans.** A chapter not listed in `SUMMARY.md`, or a link to a chapter that does not exist.

`agent-docs/` must not exist on `main`. If it does, report it as one row: move the lasting parts
into the book, file the remaining work in Linear (`linear-work` skill), and delete it. A plan or
deep dive at the repo root gets the same row.

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
| 1 | book / comment / docs | `path:line` | One sentence. | `path:line` of the code that disproves it, or the command whose output does. | `stale`, `contradicted`, `orphaned`, `restates-code`, `missing` | The smallest edit that makes it true, or `delete`. | high / medium / low |

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
3. In the book, keep a superseded design's reasoning under Development rather than deleting it.
4. Return an updated mini-ledger marking each row `fixed`, `deferred`, or `not actionable`.

Documentation-only changes belong in their own small PR, separate from the code they describe.
