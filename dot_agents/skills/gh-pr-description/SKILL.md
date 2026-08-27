---
name: gh-pr-description
description: Write or update a concise GitHub PR description without overwriting useful content.
---

# GH PR Description

Adapted from `ishandhanani/dotfiles@7b54a8c` (`agents/skills/gh-pr-description`).

Use this skill to keep PR summaries concise and walkthroughs complete, accurate, and stable. Fetch the existing body first, update only the managed description block, and preserve all other content verbatim.

## Workflow

1. Resolve the PR.
   - Use a provided PR URL or `OWNER/REPO#N`.
   - Otherwise use the current branch: `gh pr view --json number,url,title,body,baseRefName,headRefName`.
2. Gather concise evidence.
   - Read the existing PR body.
   - Inspect the current diff and the relevant implementation, not only filenames and commit subjects.
   - Compare the existing walkthrough against the current code claim by claim. Update or remove anything invalidated by every new change.
   - Pull linked issue IDs from branch name, PR title/body, commit messages, or user context.
3. Compose the managed block.
   - Keep intro to exactly 2 sentences: what changed and why it matters.
   - Keep implementation to 3-6 bullets.
   - Keep a complete reviewer-facing technical explanation inside the walkthrough `<details>` block; follow [Walkthrough Content](#walkthrough-content).
   - Treat `Before and After` as opt-in. Before adding it to a PR that does not already contain it, ask the user and wait for explicit approval. Use it primarily for concise, comparable error, diagnostic, or output snippets; do not turn general behavior changes into a table. Update an existing approved section without asking again.
   - Include validation, even if it is `Not run (reason)`.
   - If associated with a Linear ticket, include `CLOSES: <TICKET-ID>`; do not add the full Linear URL.
   - Add a benchmark section at the bottom only when benchmark results were actually collected. Present headline results as a compact Markdown table with units and baseline deltas where applicable.
   - If one simple graph of a metric comparison or trend would be useful, create exactly one, upload it to a gist, and embed its raw image URL below the table. Prefer SVG for crisp text and gist compatibility; skip the graph when the table is sufficient.
4. Update the body.
   - If the body already has the managed markers, replace only that block.
   - If no managed block exists, put the managed block at the top and leave the old body below it.
   - Write with `gh pr edit --body-file <file>` after composing from the fetched existing body.
5. Report what changed and the PR URL.

Except for first adding `Before and After`, no separate approval is required when the user asks to update the PR description. If the user asks for a draft only, do not write.

## Managed Block

Use exactly these hidden markers:

```md
<!-- codex-pr-description:start -->
Two-sentence intro. The second sentence should explain why the change matters.

CLOSES: DYN-123

### How This Was Implemented
- Short implementation bullet.
- Another bullet only if it adds signal.

<details>
<summary>Walkthrough</summary>

#### Mental model

Explain the behavior in plain language, then include a Mermaid flow or sequence diagram when state, data, or ownership crosses three or more components.

#### State and ordering

Explain state, scores, precedence, tie-breaking, and a small concrete example when applicable.

#### Request lifecycle

Trace the relevant success and failure paths and explain why new helpers or indirection exist.

#### Boundaries and limitations

State what remains unchanged, current approximations, and known sharp edges.

</details>

### Validation
- `command` or `Not run (reason)`.

### Benchmark Results
- Only include this section if benchmarks were run.
- Present headline numbers as a compact Markdown table with units and baseline deltas where applicable; link artifacts/logs instead of pasting raw output.
- When one graph adds useful signal, create it from the summarized benchmark data, upload the image with `gh gist create`, resolve its raw URL with `gh api gists/<gist-id>`, and embed it below the table.
<!-- codex-pr-description:end -->
```

## Walkthrough Content

Treat the walkthrough as the durable explanation a reviewer needs to understand the implementation without reconstructing it from the diff. Include every applicable item:

- Start with the simplest mental model and the observable outcome.
- Use one compact Mermaid flowchart for multi-component state/data/ownership flow, or a sequence diagram when lifecycle ordering is central.
- Explain state ownership, scoring or ordering semantics, precedence, ties, and one small numeric/table example when useful.
- Explain why each non-obvious helper, adapter, or indirection exists and which existing machinery it reuses.
- Trace the request/event lifecycle, including when state is committed and how cancellation or failure behaves.
- State what the change deliberately does not alter.
- State current approximations, limitations, and sharp edges. Never leave superseded design claims in place.
- Name relevant files/functions, but do not turn the walkthrough into a file inventory.

Omit inapplicable headings, not applicable information. Refresh the walkthrough every time the PR changes enough to affect any explanation, diagram edge, ordering rule, validation statement, or limitation.

## Anti-Slop Rules

- Do not delete content outside the managed block.
- Do not paste long logs, benchmark dumps, or generated artifacts into the body; link or summarize them.
- Do not list every changed file; group boring mechanical changes.
- Do not claim validation that was not run.
- Do not add a `Before and After` section without explicit user approval. If approved, compare the same scenario on both sides, prefer concrete error/output snippets, and label meaningful environment or methodology differences.
- Do not add `Benchmark Results` without real benchmark data.
- Do not add more than one benchmark graph or graph data already clear from the table.
- Do not upload raw logs, secrets, or unpublished data to a gist; graph only the aggregate values already included in the PR description.
- Do not add speculative follow-up work.
- Do not repeat the same issue link in multiple sections.
- Do not use more than 6 implementation bullets or one walkthrough diagram. Walkthrough detail is allowed when each paragraph explains current code behavior.

## Existing Body Safety

- Preserve unknown sections verbatim.
- Preserve checklists, reviewer instructions, release notes, and benchmark tables unless the user explicitly asks to rewrite them.
- If the existing body is empty, write only the managed block.
- If the existing body has a previous non-managed description, keep it below the managed block until the user asks to remove it.

## Fallback

If `gh` cannot resolve the PR, ask for a PR URL or `OWNER/REPO#N`.
If `gh auth status` fails, ask the user to run `gh auth login`.
