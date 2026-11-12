---
name: linear-work
description: File, plan, claim, and close work items in Linear the same way every time. Use when you create or update tickets, plan a project or milestone, capture follow-ups from a PR or review, pick the next ticket for an agent swarm, or report the result of a ticket.
user-invocable: true
---

# Linear work

Linear holds the owner's work items: plans, follow-ups, and the queue that agents work through. The owner sets what matters (priority) and approves projects and plans. Agents create, claim, update, and close tickets inside those limits.

**The Charter comes first.** Before you file, claim, approve, or close anything, fetch the Charter document and the newest initiative update: `list_documents` with the query `Charter`, then the latest initiative status update, which holds the standing orders. The Charter holds the rules that change: how to pick work, the approval gate and its labels, the signature line that starts every comment and issue you write, the claim and release comments, and the merge rules. Where this skill and the Charter disagree, the Charter wins. This skill covers what the Charter leaves out: the hierarchy, the fields, the issue body, and how to file and close.

Reach Linear through the `linear` MCP server (see `~/.agents/ENVIRONMENT.md`). If the server is not connected, stop and tell the owner. Do not use the API another way.

## The team

Every ticket lives in the owner's private team. Find it with `list_teams`: it is the only team the key returns. If `list_teams` returns more than one team, ask the owner which one to use. Do not guess.

**Never write the team's name, its key, a ticket identifier, or a Linear URL into a repository**: not in code, comments, docs, commit messages, or branch names. Repositories can be mirrored publicly, and the tracker is private. A PR title or body in the owner's own repositories may name its ticket, so that Linear links the two. Also put the commit or PR URL on the ticket (`create_attachment` or a comment).

Do not create or change workspace-level objects: initiatives, workspace labels, workspace templates. The workspace is shared with a whole company. Keep everything inside the team.

## Hierarchy

| Linear object | Meaning | Who sets what |
|---|---|---|
| Project | One workstream with one goal, usually in one repository: a feature (cells), a cleanup (a PR's follow-ups), a docs refactor. Name it `<Repo>: <workstream>`, for example `GDL: cells`. | The owner sets its priority. Agents write its description: goal, repository, scope, out of scope, and the book chapters that describe the design. |
| Milestone | An ordered stage inside a project. A stage finishes before the next one starts, unless its tickets are independent. Milestones have no priority; their order is the plan. | Agents propose; the owner reorders. |
| Issue | One reviewable change: one PR, or one commit series on `main` where the repository works that way. | The owner sets priority. Agents write the body and set the estimate. |
| Sub-issue | A follow-up found while working on the parent and deferred, or one step of a parent that is too big for one change. | Agents. |
| Project document | A plan or design in progress that is too long for an issue body, and the reports about the work (a bake-off, a measurement campaign). | Agents. |

Durable knowledge does not stay in Linear. Once the work is built, how it works and why goes into the repository's book (see "Documentation" in `AGENTS.md`). A ticket is a brief and a record, not documentation.

## Fields

- **Priority**: set by the owner on projects and issues. Urgent, High, Medium, Low. An agent that creates an issue leaves priority empty.
- **Estimate**: the size of the issue. The Charter maps the number to a time limit for an issue in progress. Set it when you file the issue. It is not a token budget.
- **Labels**: team-scoped only. The team's `area` label group (multi-select) has one child per crate or component, named exactly like it (`gdl-runtime`, `docs`, `velo`); use it for where the change lands, and add a missing child to the group with the team's id. Use the existing `Bug`, `Feature`, `Improvement`, `Test`, and `idea` labels for the kind of work. The Charter lists the labels that gate work: the autonomy label that every issue needs before Todo, the label for the tool that did the work, and the rest. Do not create a label at workspace level.
- **Assignee**: the owner. An agent working a ticket says so in a comment. It does not reassign the ticket.

## Status

The Charter's approval gate defines each status and who may move an issue. For filing, two rules:

- Every ticket an agent creates goes to **Triage**, with two exceptions: an item the owner already deferred, or a bare idea with no plan to build it, goes to **Backlog** with the `idea` label.
- An agent does not move its own ticket to Todo. Send the steward the ticket's identifier and start nothing before approval.
- A ticket that you cancel or mark as a duplicate gets a comment that says why, or links the duplicate.

A ticket that waits on something outside it (an owner decision, an owner action, another team) gets the waiting label that the standing orders name, on the root of a chain only. For an owner decision, add an `## Open question` section to the body, with the options. A ticket that waits on another ticket uses Linear's blocked-by relation instead, without the label. Remove the label when the wait ends.

## Issue body

The ticket is the whole brief. A fresh agent must be able to start from it without the conversation that created it. If a design ruling governs the work, copy its text into the body. The plan document it came from may be deleted.

```markdown
## Goal
One or two sentences: the outcome, not the steps.

## Acceptance
- [ ] The failing test to write first, and what it asserts.
- [ ] Each further observable result.
- [ ] Docs: the book chapters, README, and LIMITATIONS entries to update.

## Context
Key files (`path:line`), the governing design rulings (verbatim), and the measurements or failures that motivate the work.

## Out of scope
What a reasonable agent might do here but must not.

## Dependencies
Tickets that must finish first (use Linear's blocked-by relation too).
```

Write titles in the imperative, under 70 characters: "Seal the crossing marker traits", not "Crossing traits are not sealed".

## Claim the next ticket

Pick and claim as the Charter says ("How to pick work" and "The agent loop"): which projects and labels are open to you, the order, and the `claim:` and `release:` comments. In the claim comment, name your model, your branch, and its base commit. Claim one ticket per agent at a time.

## Spend

There are no token budgets now. Report your token or compute use in the closing comment when your tool shows it.

## Close a ticket

When the change merges, move the ticket to Done and comment with:

- the commit or PR link, also as an attachment;
- the tests that you ran and their results (counts, not "all green");
- the review rounds and what they found;
- your token or compute use, when your tool shows it;
- each follow-up you deferred, as a new sub-issue in Triage.

## Capture follow-ups

A PR or review that defers work files each deferred item as its own issue, in Triage, in the project for that workstream. Do not leave the list only in a PR description. Group a PR's follow-ups under one milestone named after the change, or under a parent issue when there are only a few.

## Ask the owner

Ask through `bb sgc ask` with `--issue <ticket>`: one scenario per question, the outcomes as options, your recommendation first. Ask when two projects or milestones could each own a ticket, or when a ticket needs a decision that no ruling covers and that changes its approved scope. Implementation details inside an approved project are yours and the steward's to decide. Do not ask for priority values: leave the field empty, and the owner sets it in triage.
