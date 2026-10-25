---
name: simple-english
description: Write or check short, plain text for commit messages, PR and issue descriptions, READMEs, reports, book chapters, docstrings, and code comments. Use it before any of these ship. Do not use it for chat replies.
---

# Simple English

Write the shortest text that gives the reader every fact they need, in the order they need it. Then stop.

Adapted from `ai-dynamo/rhino@a63a717` (`.agents/skills/simple-english`), MIT licensed. Sentence rules follow ASD-STE100 Issue 9; the full catalog is in `references/rules.md`.

## Authorship

You draft the text; the user signs it.

- Write only facts from the diff, the code, the conversation, or a source you read. Do not invent a motive, a benefit, a risk, or a design reason.
- In your reply, name each sentence that states something you inferred. The user confirms it before the text ships.
- Keep your own suggestions out of the text. Put them in your reply.
- The text has no more ideas than its input. Grammar can add words; it cannot add ideas.
- When you edit the user's text, change only the words that break a rule. Keep their order and emphasis.

## Process

1. List the facts. Nothing else goes in.
2. Pick the artifact and its budget (below).
3. Put the answer first: what changed, what was found, what to do.
4. Draft with the rules below.
5. Cut. Run the padding checklist. Delete each sentence the reader would not miss.
6. Report the inferred sentences in your reply.

When you check text instead of writing it, list each problem as: quote, rule, fix.

## Budgets

These are defaults. Going over needs a reason the reader would accept, such as a migration step or a breaking change.

| Artifact | Budget |
|---|---|
| Commit subject | 50 characters, 72 at most. Imperative, no period. |
| Commit body | None if the subject says it all. Otherwise one to three short paragraphs: the problem, the change, the evidence. Wrap at 72. |
| PR description | Under 150 words plus a test line: problem, change, how you checked it. |
| Issue description | Under 120 words: observed, expected, steps to reproduce. |
| Code comment | One line, rarely three. The reason, or a constraint the code hides. |
| Doc comment, internal item | One summary line, only when the reason is not obvious. |
| Doc comment, public item | Summary line; then Errors, Panics, Safety where they apply; then one example. Under 15 lines. |
| README section | Under 150 words, one topic. Commands over prose. |
| Report or design doc | Opening paragraph under 80 words; TL;DR of three to five bullets; details below. |
| Book chapter | One topic and one kind (below). No word cap, but every paragraph carries a fact the code does not show. |

## Reports, design docs, and status updates

The first screen decides whether anyone reads the rest. Write it for a reader who stops there.

1. **Opening paragraph**: the problem or question, why we care, and how we tested or decided. Three sentences is typical.
2. **TL;DR**: the result and the recommendation, in three to five bullets. Lead with the decision; give one number per claim.
3. **Details**: method, results tables, findings, caveats, for the readers who want them.
4. **Provenance last**: source files, thread IDs, commit hashes, and run dates go in a closing section, not the opening.

A recommendation never sits only at the end. Worked example: `references/padding.md`.

## Rules

- Instructions: at most 20 words, one instruction per sentence, imperative, condition first.
- Descriptions: at most 25 words per sentence, one topic per paragraph, at most six sentences per paragraph.
- Active voice, simple present tense. Use "can", "will", and "must"; not "should" or "might" unless the fact is uncertain.
- One name per thing. Pick "check" or "verify" and keep it.
- Keep articles and "that". Clipped grammar is harder to read, not shorter to understand.
- No noun chains longer than three words. No semicolons, "e.g.", "i.e.", "etc.", or "and/or".
- Code, commands, paths, flags, identifiers, and quoted errors stay exact.
- Numbers beat adjectives: "p99 fell from 12 ms to 8 ms", not "much faster". A claim about speed, safety, or compatibility needs a number or a test name.
- Comments describe the code as it is. The current task, the old code, and past bugs belong in git.

## Padding checklist

Delete or rewrite on sight. Examples: `references/padding.md`.

- Preamble: "This PR introduces...", "Here is a summary of...".
- Restating the title, the diff, the signature, or an identifier.
- A closing summary: "In summary", "Overall".
- Hedges and intensifiers: "it is worth noting", "importantly", "robust", "seamless", "simply".
- Three adjectives or three benefits where one fact exists.
- An invented motive or benefit: "improves maintainability", "for clarity".
- A test list that repeats test names. Write the command and its result.
- Headings in a text under 150 words.
- Dash asides. Make two sentences.
- Explanation inside a how-to or a commit. Link to it.

## Structure by artifact

- Commit: the subject says what changed; the body says why it was a problem; the diff says how.
- PR: problem, change, check, and risk only if real.
- Doc comment: what it does, the contract the signature cannot show, one example. Rust examples use `?`, not `unwrap`.
- Book chapter: one of tutorial, how-to, reference, or explanation. Do not mix them in one chapter.

## Modes

Pragmatic is the default: domain words stay ("idempotent", "webhook"). Use strict mode when the user names STE or ASD-STE100: apply the vocabulary rules in `references/rules.md`, and say that full compliance needs the official dictionary.

## References

- `references/padding.md`: padding patterns with before and after, the slop-to-plain table, and a report rewrite.
- `references/rules.md`: the full STE catalog, the modal ladder, and the vocabulary rulings. Cite only rule numbers from this file.
- `references/checklist.md`: searchable patterns for check mode.
- `references/use-cases.md`: error messages, runbooks, incident reports, agent prompts.
