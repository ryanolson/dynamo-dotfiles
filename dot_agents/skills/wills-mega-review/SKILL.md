---
name: wills-mega-review
description: Prepare mostly working, tested work for human review with a bounded loop of independent read-only thermonuclear reviews, judged fixes, and regression tests. Use when the user asks for a mega review (wills, fable, or codex). It is optional; offer it, do not run it unasked.
---

# Will's Mega Review

Adapted from `ai-dynamo/rhino@a63a717` (`.agents/skills/wills-mega-review`).

Run this only once the feature mostly works and relevant tests pass.

## Setup

- **Range.** Pin the head commit and name the review range (`<base>..<head>`) before the first pass. Every reviewer gets the same range.
- **Reviewer.** The user may name the reviewer family: "fable mega review" means Fable, "codex mega review" means a Codex bb thread. Otherwise use a different model family from the author's: a Codex bb thread for Claude-authored work, Fable or Opus for Codex-authored work. Never the model that wrote the code.
- **Rubric.** Use the repository's own review skill when its agent file names one; otherwise the global `thermo-nuclear-code-quality-review`.

## Loop

1. Launch a fresh **read-only** reviewer on the range. It reports findings in the rubric's format and makes no edits, commits, labels, or other changes.
2. Rule on each finding before touching code, as `AGENTS.md` "Validating a claim" says: valid, partially valid, or invalid. A finding that repeats an earlier ruling without new evidence stays ruled.
3. Fix the valid blocker and major findings that are in scope, each with a test that failed first. Record minor findings and out-of-scope suggestions for the next milestone's plan; do not fix them now. Simplification ideas are advisory.
4. Run the relevant tests under a sized `timeout`.
5. Start the next pass with a new reviewer, not a reused one.

**Stop** at the earliest of:

- the pass budget: three passes for code, two for a design doc;
- a pass with no new valid blocker or major finding;
- a pass whose only findings repeat earlier rulings.

If a serious finding is still open at the budget, stop and report it with your ruling. Do not start another pass.

## Finish

1. Check that the documentation the change touches still matches the code.
2. Write one record per pass: reviewer, range, each finding with its ruling and fix commit, and what was deferred. Put it in bb thread storage or on the work branch, never in `agent-docs/` on `main`. Give its path in the report.
3. Deliver by the repository's merge rule:
   - Commit to `main`: commit and push.
   - Pull request: push the branch, add the `human-review` label, and ask for human review. Never add the label earlier.
4. End with the `AGENTS.md` status block, including the number of passes and the open findings.
