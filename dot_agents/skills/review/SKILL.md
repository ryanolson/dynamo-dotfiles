---
name: review
description: Review a plan, design, document, or code change in one pass, reporting only findings with a concrete failure case. Use for normal review. Not for risky changes (strict-review) or loops.
---

# Review

One review pass over a plan, a design, a document, or a code change. Adapted from `ishandhanani/dotfiles@7b54a8c`.

## Target

- The artifact the user names, or the one in the message just before. Do not dig through conversation history to reconstruct intent.
- Review a plan or design as written. Treat the target as a diff only when the user asks for a diff, branch, or PR review, or supplies one.
- Review the change, not the surrounding system. Raise a pre-existing problem only when it is severe, or when the change makes it worse or harder to fix, and say that it is pre-existing.

## Finding threshold

Report a finding only with a concrete, in-scope failure case: the precondition or input, the code path or plan step, and the outcome (wrong behavior, crash, data loss, a security boundary crossed, or a measurable regression). Prefer a reproduction; otherwise trace the path and show why the precondition can happen.

"A zero-length batch reaches this division and returns 500" is a finding. "A future caller might pass an undocumented shape" is not, without an in-scope caller or contract. Skip style preferences, speculative edge cases, unsupported inputs, and alternative designs. Do not recommend defensive code only for those. Keep findings about trust-boundary validation, data safety, and security.

## Pass

1. Challenge the target as a whole first: is the problem framed right, what does it assume, how could it pass its own checks and still fail end to end. For a plan or design, this is most of the review.
2. Pick the areas that apply: contracts and tests, concurrency and lifecycle, distributed consistency and recovery, architecture, performance. For a non-trivial target, read `references/areas.md` and give each active area its own subagent.
3. For a Rust change, also apply `references/rust.md`.
4. For a change to a registered hot path (`hot-paths` skill), run its guard benchmark before and after.
5. Verify every candidate finding yourself before reporting it. Subagent output is input, not the report.

Escalate to `strict-review` when the change touches concurrency, `unsafe`, registered hot paths, public interfaces, or a lifecycle across modules.

## Report

Findings first, ordered by severity. Each finding has: `file:line` or the plan step, the failure case, severity (blocker, major, minor), and a test that would fail, or the evidence that shows it. Zero findings is a pass; say so in one line. Do not pad the report with praise, summaries, or style notes.

## Completion

One pass, every reported finding verified against the threshold, and the report in the format above. This skill does not loop; `review-loop` owns loops and the review budget.
