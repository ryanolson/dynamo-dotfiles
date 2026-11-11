---
name: review-loop
description: Bring finished work to merge with a bounded loop of cross-family reviews, test-backed findings, and judged fixes. Use before the merge of approved, tracked work, when a milestone is done, or when asked for a mega review (wills, fable, codex). For untracked work, offer it first.
---

# Review Loop

Adapted from `ai-dynamo/rhino@a63a717` (`.agents/skills/wills-mega-review`).

Run this only once the feature mostly works and relevant tests pass.

## Setup

- **Range.** Pin the base commit before the first pass. Each pass reviews `<base>..<current head>`, so it sees the fixes from the passes before it, and gets the earlier rulings.
- **Reviewer.** A reviewer the user names wins: "fable mega review" means Fable, "codex mega review" means a Codex bb thread. Otherwise use the cross-family row of the "Model routing" table in `ENVIRONMENT.md` for the author's family. Never the model that wrote the code.
- **Gotchas.** Name the repo book's "Coding gotchas" chapter in every reviewer brief, when the book has one.
- **Rubric.** Use the repository's own review skill when its agent file names one; otherwise the global `strict-review`.

## Loop

1. Launch a fresh reviewer on the range, in test-backed mode (below) when it can build and run the tests, otherwise read-only. It reports findings in the rubric's format. It never edits implementation files on the implementer's branch, and adds no labels.
2. Rule on each finding before touching code, as `AGENTS.md` "Validating a claim" says: valid, partially valid, or invalid. A finding that repeats an earlier ruling without new evidence stays ruled.
3. Fix the valid blocker and major findings that are in scope, each with a test that failed first. When a valid finding is a race or a flaky test, the same fix adds or updates its lesson in the repo book's "Coding gotchas" chapter (`AGENTS.md`, "The book"). Record minor findings and out-of-scope suggestions for the next milestone's plan; do not fix them now. Simplification ideas are advisory.
4. Run the relevant tests under a sized `timeout`.
5. Start the next pass with a new reviewer, not a reused one.

**Stop** at the earliest of:

- the pass budget: up to 10 passes for code, scaled to the size of the change, and four for a design doc, counted per milestone or document across every run of this skill (read the earlier records). Starting the skill again does not reset the count;
- a pass with no new valid blocker or major finding;
- a pass whose only findings repeat earlier rulings.

At pass 6, check that the findings converge before you start pass 7. A fix made after the last pass gets a pass of its own.

If a serious finding is still open at the budget, stop and report it with your ruling. Do not start another pass.

## Test-backed findings

The reviewer has the context of each discovery; a failing test carries that context to the implementer, where a prose report must be re-derived. Use this mode whenever the reviewer can build and run the tests. A reviewer that writes a fix becomes its author, so fixes stay optional and never merge unreviewed.

**Reviewer**

- Work in your own worktree on `review/<branch>/p<pass>`, branched from the current head. Under bb, a child thread has one; outside bb, use `git worktree add`.
- For each finding you reproduce, make one commit, `review: F<n> <summary>`, that adds only a failing test, marked as skipped with the reason (`#[ignore = "F<n>: <why it fails>"]` in Rust) so the branch stays green. Run it with the skip removed, and put the failure output in the commit body.
- Optionally, add a second commit, `review: F<n> suggested fix`, on top, only for a small, local fix. Never for a design finding.
- Spend only a small budget on each reproduction. Report a finding you cannot reproduce, such as a design concern or one that needs GPUs or a cluster, as `unreproduced`; do not drop it.
- Report each finding with its severity, its commit SHAs, and whether it was reproduced.

**Implementer**

- For each test commit: cherry-pick it, remove the skip, and run it. A failure for the stated reason makes the finding valid; fix it and keep the test. A pass means the finding or the test is wrong; rule invalid or partially valid and say why.
- Take a suggested-fix commit only after reviewing it as your own code. The next pass's reviewer checks it like any other change.
- Record, for each finding: reproduced or not, the ruling, accepted or not, and the reviewer's token use when the provider reports it. These numbers decide whether this mode is worth its cost.

## Finish

1. Check that the documentation the change touches still matches the code.
2. Write one record per pass: reviewer, range, each finding with its ruling and fix commit, and what was deferred. Put it in bb thread storage, on the work branch, or, outside bb in a repository that commits to `main`, in `~/.local/state/agent-reviews/<repo>/`. Never in `agent-docs/` on `main`. Give its path in the report.
3. If a valid blocker or major finding is still open, stop here: report it, and do not merge, push to `main`, or add a label until the owner decides. Otherwise deliver by the repository's merge rule, and by the Charter's for tracked work:
   - Commit to `main`: commit and push.
   - Pull request that its driver may merge (the Charter names the repositories and the labels): merge it when the build is green on the merged tree.
   - Any other pull request: push the branch and leave the PR open for the person who merges. Add the `human-review` label where the repository has one, and only now, after the loop is clean.
4. End with the `AGENTS.md` status block, including the number of passes and the open findings.
