---
name: full-code-review
description: Perform a comprehensive code review with general and deep review passes.
---

# Full Code Review

Adapted from `ishandhanani/dotfiles@7b54a8c` (`agents/skills/full-code-review`). Rewired to call `thermo-nuclear-code-quality-review` in place of the upstream `deep-code-review`.

## Finding threshold — set before every pass

Establish this bar before invoking any child skill: include a finding only when it gives a concrete, in-scope failure case — precondition/input, affected code path, and outcome (wrong behavior, crash, data loss, security-boundary violation, or measurable regression). Prefer a reproduction; otherwise trace the path and show why its precondition is supported.

For example, “A zero-length batch reaches this division and returns 500” is a finding. “A future caller might pass an undocumented shape” is not one without an in-scope caller or contract. Skip speculative edge cases, style preferences, unsupported inputs, and alternative designs; do not recommend defensive code solely for them. Retain findings about trust-boundary validation, data safety, or security.

Run both existing skills against the same target:

1. Read and follow `../general-review/SKILL.md` completely, applying the finding threshold above.
2. Read and follow `../thermo-nuclear-code-quality-review/SKILL.md` completely, applying the finding threshold above.
3. Use the same scope and evidence for both reviews.
4. Verify and deduplicate their findings, then return one severity-ordered report.

This sets the admission bar for the consolidated report; do not copy, weaken, or replace any child skill's rubric.
