---
name: linear-work
description: File, plan, claim, and close work items in Linear the same way every time. Use when you create or update tickets, plan a project or milestone, capture follow-ups from a PR or review, pick the next ticket for an agent swarm, or report the result of a ticket.
user-invocable: true
---

# Linear work

Linear holds the owner's work items: plans, follow-ups, and the queue that agent swarms work through. The owner sets what matters (priority) and what it may cost (the token budget). Agents create, claim, update, and close tickets inside those limits.

Reach Linear through the `linear` MCP server (see `~/.agents/ENVIRONMENT.md`). If the server is not connected, stop and tell the owner. Do not use the API another way.

## The team

Every ticket lives in the owner's private team. Find it with `list_teams`: it is the only team the key returns. If `list_teams` returns more than one team, ask the owner which one to use. Do not guess.

**Never write the team's name, its key, a ticket identifier, or a Linear URL into a repository**: not in code, comments, docs, commit messages, branch names, or PR text. Repositories can be mirrored publicly, and the tracker is private. Link in the other direction. Put the commit or PR URL on the ticket (`create_attachment` or a comment).

Do not create or change workspace-level objects: initiatives, workspace labels, workspace templates. The workspace is shared with a whole company. Keep everything inside the team.

## Hierarchy

| Linear object | Meaning | Who sets what |
|---|---|---|
| Project | One workstream with one goal, usually in one repository: a feature (cells), a cleanup (a PR's follow-ups), a docs refactor. Name it `<Repo>: <workstream>`, for example `GDL: cells`. | The owner sets its priority. Agents write its description: goal, repository, scope, out of scope, and the book chapters that describe the design. |
| Milestone | An ordered stage inside a project. A stage finishes before the next one starts, unless its tickets are independent. Milestones have no priority; their order is the plan. | Agents propose; the owner reorders. |
| Issue | One reviewable change: one PR, or one commit series on `main` where the repository works that way. | The owner sets priority and estimate. Agents write the body. |
| Sub-issue | A follow-up found while working on the parent and deferred, or one step of a parent that is too big for one change. | Agents. |
| Project document | A plan or design in progress that is too long for an issue body, and the reports about the work (a bake-off, a measurement campaign). | Agents. |

Durable knowledge does not stay in Linear. Once the work is built, how it works and why goes into the repository's book (see "Documentation" in `AGENTS.md`). A ticket is a brief and a record, not documentation.

## Fields

- **Priority**: set by the owner on projects and issues. Urgent, High, Medium, Low. An agent that creates an issue leaves priority empty.
- **Estimate**: the token budget, in millions of tokens, on the exponential scale: 1, 2, 4, 8, 16, 32, 64. It covers the whole ticket: implementation, tests, review rounds, and fixes. The owner sets it. An agent that creates an issue writes a suggested budget in the body (`Suggested budget: 8M`) and leaves the field empty. For calibration: one Opus implementation of a change of about 500 lines, with two or three adversarial review rounds, cost about 4M to 8M tokens.
- **Labels**: team-scoped only. The team's `area` label group (multi-select) has one child per crate or component, named exactly like it (`gdl-runtime`, `docs`, `velo`); use it for where the change lands, and add a missing child to the group with the team's id. Use the existing `Bug`, `Feature`, `Improvement`, `Test`, and `idea` labels for the kind of work. Do not create a label at workspace level.
- **Assignee**: the owner. An agent working a ticket says so in a comment. It does not reassign the ticket.

## Status

| Status | Meaning |
|---|---|
| Triage | Proposed by an agent. The owner has not accepted it. |
| Backlog | Accepted, but not ready for a swarm: no priority, no estimate, or open design questions. |
| Todo | Ready. It has a priority, an estimate, and a complete body. A swarm may claim it. |
| In Progress | Claimed. The claiming comment names the agent, the bb thread or session, and the branch. |
| In Review | The change is complete and in review: a PR, or adversarial review rounds. |
| Done | Merged to `main`. The closing comment links the commit or PR. |
| Cancelled / Duplicate | Not done. The comment says why, or links the duplicate. |

Every ticket an agent creates goes to **Triage**. Only the owner moves a ticket to Todo.

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

Suggested budget: 8M
```

Write titles in the imperative, under 70 characters: "Seal the crossing marker traits", not "Crossing traits are not sealed".

## Claim the next ticket

A swarm picks tickets in this order:

1. Only `Todo` tickets with an estimate, and with no open blocker.
2. Project priority, highest first. A project without a priority comes last.
3. Issue priority, highest first.
4. Milestone order within the project.
5. The ticket's position in the project (`sortOrder`).

To claim, move the ticket to In Progress and comment: agent and model, bb thread or session id, branch, and base commit. Claim one ticket per agent at a time.

## Budget

Track spend against the estimate: tokens from the session's context reports where available, otherwise review rounds and wall-clock time.

- At about 75% of the budget, comment with the state and what remains.
- At 100%, stop. Commit nothing that does not pass the suite. Comment with what is done, what remains, and the spend, then move the ticket back to Todo. The owner decides whether to raise the budget, split the ticket, or drop it.
- Do not split a ticket to get around its budget.

## Close a ticket

When the change merges, move the ticket to Done and comment with:

- the commit or PR link, also as an attachment;
- the tests that you ran and their results (counts, not "all green");
- the review rounds and what they found;
- the spend against the budget;
- each follow-up you deferred, as a new sub-issue in Triage.

## Capture follow-ups

A PR or review that defers work files each deferred item as its own issue, in Triage, in the project for that workstream. Do not leave the list only in a PR description. Group a PR's follow-ups under one milestone named after the change, or under a parent issue when there are only a few.

## Ask the owner

Ask when two projects or milestones could each own a ticket, when a ticket needs a design decision that no ruling covers, or when a budget is clearly wrong before you start. Offer options. Do not ask for priority or budget values: leave them empty and the owner sets them in triage.
