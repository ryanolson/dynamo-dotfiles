# Agent instructions review

Status: in progress
Updated: 2026-11-11

We want the global agent file (`dot_agents/AGENTS.md`) and the shared skills to match how the agents are actually used, and to follow current Claude and Codex guidance. Agents were ignoring rules the owner had overridden by hand, looping on reviews, and writing too much. This review read the owner's memories, Claude and Codex transcripts, bb prompt history, and the agent files in six repositories, plus current vendor documentation. It is meant to run again on a schedule.

**TL;DR**

- The owner overrode four global rules by hand in most repositories: draft PRs first, fixed test timeouts, unbounded review, and plans kept in Linear. The file now matches what they asked for.
- Reviews looped because the skills required it: `wills-mega-review` applied every finding until a review was clean, and thermo treated "a simpler design exists" as a blocker. Both are now bounded at three passes for code and two for a design doc.
- About 21 repeated questions were "did it land, was it reviewed, where are we". Every unit of work now ends with a status block.
- `simple-english` had no length limits. It now has per-artifact budgets, puts the answer first, and is 104 lines instead of 336.
- Prose cannot enforce the no-attribution rule: the harness adds `Co-Authored-By` anyway. A settings change is needed (see Open decisions).

## What changed

| Commit | Change |
|---|---|
| `c09a3d6` | Plan store chosen per project (`Plans: agent-docs` by default, or `Plans: Linear`), with lifecycle rules for each. Commit to `main` by default. Test timeouts sized to the measured run. Review budget. Status block. A "Running under bb" section. A documentation pass each release, and short doc comments for public items. The file overrides memory. Fleet and secrets moved to `ENVIRONMENT.md`. Hard wraps removed. |
| `fc0dcf4` | `simple-english`: budgets, a report structure (problem, why, method, then TL;DR), a padding checklist, and the STE catalog moved to `references/rules.md`. |
| `27e14c0` | Review skills: a bounded loop with a ruling on each finding, one-pass reviewers, advisory simplification, and models sized to the task. |
| `9b7821e` | Session start detects bb first; under bb, delegate code work to child threads. Draft PRs for repositories other people commit to. Book appendices. |
| `277cada` | `attribution` set to empty in `~/.claude/settings.json` through a modify script. |
| `d5f0927` | Loop gaps a Codex review found: moving review range, budget across runs, no push with an open blocker. |
| `f0a2b77` | Model tiers in `AGENTS.md` with a dated routing table in `ENVIRONMENT.md`. Review table ordered by when. Test-backed findings in `wills-mega-review`. |

## Round of 2026-11-11: Fable 5.2 and the Charter

The environment now targets Fable 5.2 and Opus 5.5. This round read the vendor's prompting guides for both models, the Charter and the standing orders in Linear, and the review skills.

**Changed**

- The Charter had moved ahead of these files. `AGENTS.md` now points to the Charter and the standing orders, and says that they win for tracked work, instead of copying their numbers. `linear-work` defers to the Charter for picking, claiming, labels, estimates, and merging. Its token-budget rules are gone: there are no budgets now, and the Fable 5.2 guide found that a budget shown to the model changes what it spends.
- A new section, "What needs my approval", names the steps that need no approval. Fable 5.2 follows cautious rules closely and treats a rule with "never" as absolute. `review-loop` said "never run unasked", which stops the review that the Charter requires before a merge.
- Questions for the owner go through `bb sgc ask`. The rule to write options inline in chat is gone.
- Four rules follow sections of the Fable 5.2 guide: finish the whole task, who owns the code, what delegation costs, and a line of intent with a recap that stands on its own.
- `review` no longer starts one subagent for each area by default.
- Model routing: Fable 5.2 joins the top tier at `high`. The owner's effort settings on the other rows did not change.
- `pr-babysitter` names tiers instead of two pinned model names that newer models have replaced.

**Found, not changed**

- One host's source checkout was 23 commits behind its remote, so its agents read the rules of 2026-10-25. A host that lags makes every rule change a no-op there. Add `git -C "$(chezmoi source-path)" status -sb` on each host to this review.
- Effort: the vendor guides start Fable 5.2 at `high` and Opus 5.5 at `medium`, and keep `xhigh` and `max` for measured gains. The owner then set the split: Fable 5.2 at `high` for design, orchestration, review, and rulings, and Opus 5.5 at `high` for implementation, down from `max`. No comparison on our own tasks exists yet. It is an open idea in the tracker.
- `simple-english` has hard caps on sentence length, which the Fable 5.2 guide says can make prose choppy. A sample of recent book text read well, so the caps stay. Check again after a month of Fable 5.2 text.
- `strict-review` is about 320 lines, with four lists that overlap and strong emphasis throughout. A model that follows instructions closely can over-flag from it. Compare a trimmed version on a known change before replacing it.

## Evidence

**Usage shape (October 2026).** About 187 Claude sessions with transcripts and 276 Codex sessions. Most Codex sessions are reviews started by bb or the Claude Codex plugin, so Codex mostly reads the file as a reviewer. bb ran 156 threads: 98 children under 9 parents, one coordinator with 75 children. Most multi-agent work runs through bb, not the Agent tool.

**Rules overridden by hand.**

- PRs: four repositories told agents to commit to `main` (chezmoi, dynamo-gdl, velo, agentic-control).
- Timeouts: "a 10min timeout for single unit test is a bit silly" (dynamo-gdl, 2026-10-16), then a request that the value not be fixed.
- Review: "why are we doing a 3rd thermo review?" and "do not get stuck in review death spirals… 2-3 passes". One milestone got 8 to 20 thermo runs on 2026-10-15.
- Memory: a memory said "don't commit" and contradicted the global file. The owner said to follow the global file.

**Documentation.** velo's rules work: `agent-docs/` never reaches `main`, and 38 of 51 commits since its book landed updated `docs/`. rhino has no written rule. Its code cites `agent-docs/` files that do not exist on `main`, and its book carries PR numbers. dynamo-gdl and agentic-control keep `agent-docs/` on `main` as their roadmap. It grew without a limit (one plan reached 275 KB with 102 addenda, one task card 1.29 MB), because the old addendum rule had no point of consolidation. The new `Plans: agent-docs` rules add a status line, an index, a size trigger, and deletion when work ships.

**bb boilerplate.** 70 of 98 child briefs pasted the same 12 KB block of repository rules, because parents could not count on children reading the agent files. ACP providers such as Antigravity do not read `~/.claude/CLAUDE.md` or `~/.codex/AGENTS.md`.

**Vendor guidance, checked against Claude Code 2.1.310.** Keep each CLAUDE.md under about 200 lines. Explain why instead of using capitals, and keep emphasis for real invariants. Put must-hold rules in hooks or settings, not prose. Move procedures into skills. Codex stops adding project files at 32 KiB (`project_doc_max_bytes`). Claude reads `AGENTS.md` only when no `CLAUDE.md` exists. `/doctor prompt-audit` audits instruction files.

## Open decisions

1. **Attribution setting.** Done in `277cada`. Was: set `"attribution": {"commit": "", "pr": ""}` in `~/.claude/settings.json`. That file is not managed by chezmoi. A `modify_` script, like `modify_dot_npmrc`, could set only this key. The docs do not say that Claude reads `~/.claude/settings.local.json` at user level, so the permissions chezmoi deploys there may have no effect. Test this.
2. **bb-wide instructions.** Deploy the global file to the bb server's data directory, so ACP providers get it. That would let briefs stop pasting rules.
3. **`~/.claude/CLAUDE.md` as an import, not a symlink.** Cowork skips a symlinked user file, and Claude cannot edit through one. An `@~/.agents/AGENTS.md` import avoids both.
4. **Review skill set.** Done on 2026-10-25: `review`, `strict-review`, and `review-loop`. Was: merge `general-review`, `rust-code-review`, and `full-code-review` into one `review` skill with references. Cut thermo to about 80 lines. Rename velo's `thermonuclear-review` so it stops taking the global trigger.
5. **Repository files.** dynamo-gdl and agentic-control need a `Plans:` line and a consolidation pass. velo needs `Plans: Linear`, and the repositories without one need an `AGENTS.md` (velo, kvbm) or a `CLAUDE.md` (rhino, agentic-control). agentic-control calls mega review required. kvbm keeps `ACTIVE_PLAN.md` at the root. dynamo uses `agent-docs/` for something else.
6. **Public mirrors.** A repository with a public mirror publishes its `agent-docs/`. Check what each mirror filter publishes before the next push.
7. **Memory cleanup.** Delete memories that repeat the global file or are stale (listed in the session report), and move project state out of memory.
8. **Stale model names.** Done on 2026-11-11: `pr-babysitter` names tiers. Was: two pinned `gpt-5.6-*` names in the skill.

## Running this review again

- `bb prompt-history` keeps only the newest 200 prompts. Snapshot it at least weekly with `agent-instructions-review/bb-prompts.py`, or the history is lost.
- `agent-instructions-review/claude-transcripts.py <out-dir>` and `codex-transcripts.py <out-dir>` extract the typed user messages and per-session statistics. Run them with `python3 -I`.
- Look for corrections typed more than once, rules a repository overrides, memories that contradict the file, and boilerplate that briefs paste.
- Check vendor guidance again, and run `/doctor prompt-audit`.
- Refresh the "Model routing" table in `ENVIRONMENT.md`: new models, release notes, and a small bake-off for a new candidate.
- Read the test-backed review numbers recorded by `review-loop` (reproduced, accepted, tokens) and decide whether to keep the mode.
- Measure the file in words and bytes, not lines.
- Run `repo-skills` status on each public team repository (velo, rhino) and resolve drift with the owner.
