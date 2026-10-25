# Agent Instructions — Ryan Olson

Canonical source: `dot_agents/AGENTS.md` in the repository returned by `chezmoi source-path`. `~/.agents/AGENTS.md` is a deployed copy. `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` link to that copy.

A repository's own `AGENTS.md` / `CLAUDE.md` **overrides this file** wherever the two disagree. This file overrides agent memory: when a memory contradicts it, say so and follow this file.

## Who I am

Rust and Python systems engineer. Datacenter-scale distributed inference serving — Dynamo, rhino, kvbm, velo, roundhouse. Performance engineering is the core competency, so treat performance as a correctness property rather than a follow-up.

## Session start

1. Read the repository's own agent file and treat it as authoritative over this one.
2. `git worktree list` — know the checkout layout before touching a build directory or a venv. A bb thread works in its own worktree, not under `~/repos`.
3. Before selecting workstation tools or changing the environment, read `~/.agents/ENVIRONMENT.md`. It maps capabilities, secrets, command discovery, and source ownership.
4. Only when the task changes agent configuration, resolve the agent and its home: `CLAUDECODE` means Claude Code (`${CLAUDE_HOME:-~/.claude}`), `CODEX_*` means Codex (`${CODEX_HOME:-~/.codex}`). Under bb (`BB_THREAD_ID` set), `bb thread show "$BB_THREAD_ID" --json` names the provider. Other tasks do not need this step.

## Environment ownership and updates

- Before editing an installed skill, script, command, configuration, or plugin, identify its owner: resolve symlinks, then `chezmoi source-path <target>`. A path under `~` is not proof of ownership. Edit the owning source, never only the deployed copy, and never a plugin cache.
- Follow "Update a managed capability" in `ENVIRONMENT.md`: validate, preview and apply only the intended targets, check the deployed result, then commit and push in the owning repository. Apply alone is incomplete.
- Match the requested scope. A command-discovery task ends after relevant help is checked; it does not authorize service repair, installation, or target changes.

## How I work

- **Direct path first.** Make the smallest change that satisfies the request. Do not refactor working code in passing, and do not add abstraction for a second caller that does not exist yet.
- **Reproduce before you theorize.** No long explanations of what might be wrong. Get the failure in front of you first.
- **Show numbers.** Logs, metrics, benchmarks. "It is faster" is not a result.
- **Profile before guessing** when the question is performance.
- **Amend a design on evidence.** When a measurement or a failing test contradicts a design, change the design; do not defend it.

## Validating a claim

**Write the test first, then rule, then fix.** This applies to review findings, bug reports, hypotheses about behavior, and "I think X is broken" of any kind. A claim confirmed by reading code is an opinion; a claim confirmed by a failing test is a fact, and the same test proves the fix.

1. **Write a test that fails** for the reason the claim says it should. If it passes, the claim is wrong — or the test does not exercise what the claim is about, which is worth knowing first.
2. **If it cannot be tested, make it testable.** A defect no test can reach usually means the seam is in the wrong place. Add the accessor, extract the pure function, split the type. That change is part of the fix, not a detour. Prefer additive, behavior-preserving changes so the failing test is unambiguously about the defect.
3. **Rule on the claim**: *valid*, *partially valid* (the defect is real but the described mechanism is wrong — spell out the correction), or *invalid*. An external reviewer being mistaken is an ordinary outcome.
4. **Only then fix it**, and keep the test.

The order is about evidence, not about stopping — validating and fixing in one pass is the normal case. Ask only when the finding turns out to be a design question, the fix is far larger than the defect, or two valid remedies point different ways.

Where validation genuinely lands before the fix, mark failing assertions `#[ignore = "<finding>: <why it fails>"]` rather than leaving the suite red or deleting the evidence, and keep passing control tests live — they are what prove the failing ones are not tautological. But be honest: **an ignored test enforces nothing.** Removing the ignore is the first step of the fix, not a cleanup afterwards.

Fixing the described mechanism rather than the actual one leaves the defect in place behind a passing test. That is worse than not having looked.

## Bounded test runs

Always run tests under a coreutils `timeout`, sized to the run: two to three times the last measured time of the same target, rounded up to the next minute. Build first (`cargo test --no-run`) so the limit does not have to cover compilation. Keep measured times in agent memory or the repo's agent file, not in this file. With no measurement yet, start at `timeout 300` for a targeted run and `timeout 900` for a workspace, then tighten.

The reason is not slow tests — a hung test hangs the entire runner, silently, and the sessions most likely to hang one are exactly the adversarial sessions that mutate timeout and deadline code on purpose. A tight limit shows a hang at once; a large fixed one hides it.

On exit 124, suspect the newest test or the mutation just applied, and re-run the suspect binary with `--test-threads=1 --nocapture` under a short timeout to name the hang. When a target gets slower, find the cause before you raise its limit.

## Compatibility policy

**While every caller lives inside the workspace**, the compiler can find all of them, so write no code that exists to keep an old caller working. Change an interface and delete the old form; change the callers. No second entry point, forwarding wrapper, re-export, or type alias for the old name.

Never name a thing `legacy`, `old`, `v1`, `compat`, `deprecated`, or `_unused`. Such a name asserts that two forms exist when only one must. Delete the thing, or delete the prefix and merge it with the live form. Two spellings of one rule drift apart — that is how a `legacy` shard derivation and a `serving` one came to disagree on an expert-count check.

Keep code a design document names as deliberately unwired, and say where that document is.

**Once a crate is published or has external consumers this inverts, and semver discipline wins.** Breaking changes need a version bump; `=` pins are load-bearing and must not be relaxed to carets; new trait methods need default implementations, because a bare one breaks every out-of-tree impl; removing a public item is a major bump. When in doubt about which regime applies, check whether anything outside the workspace can name the symbol.

## Documentation

- **Every change leaves the documentation it touches consistent with the code**: README, module docs, doc comments, examples, file trees, build and run commands. Remove references to symbols you deleted or renamed.
- **Every release gets a fresh documentation pass** over the whole codebase with the `docs-sweep` skill, fixing the rows rather than only reporting them. It catches the drift no single change owned.
- **Comments say why**: the decision and the failure it avoids. A comment that restates the code is noise. History belongs in `git`.
- **Public items another crate or package uses carry a short doc comment**, because that reader cannot see the code: one summary line, then errors, panics, safety, or an example only where the signature does not make them obvious. Internal items get a comment only when the reason is not obvious.
- Write the book, READMEs, comments, commit messages, and PR and issue descriptions with the `simple-english` skill. Not conversation.

### The book

Every Rust project past its first weeks has an mdBook in `docs/`, modelled on `../velo/docs`: its sections, its `book.toml` (`create-missing = false`, mermaid, linkcheck), `scripts/build-book.sh`, and `development/documentation.md`, which each book copies and adapts. A project without a book uses its README and `docs/` the same way.

| Text | Place |
|---|---|
| How a subsystem works, and why, with the rejected alternative | A book chapter |
| Measured numbers, with the command and setup | A chapter under Operations |
| Rejected designs and negative results | A chapter under Development |
| A hard lesson ("X breaks when Y") | A test; its doc comment can be long |
| Roadmap, plans, rulings not yet built | The project's plan store (below) |
| Work-in-progress notes, review ledgers, handoffs | The work branch or bb thread storage |

The book keeps only what lasts. No plans, phase or milestone labels, ruling numbers, PR numbers, ticket identifiers, or handoff notes. Each behavior change updates the chapters that describe it and the top-level README, which stays short and points into the book. Code comments and the book never cite `agent-docs/`: its notes are deleted when their work ships.

### Plans: per-project store

Each project's agent file declares `Plans: agent-docs` or `Plans: Linear`. With no declaration, use `agent-docs`.

**`Plans: agent-docs`** — `agent-docs/` on `main` is the roadmap and decision record, and a fresh agent must be able to read it in one pass:

- `agent-docs/README.md` lists every live note with a one-line status.
- Each note opens with `Status:` (planned, in progress, done, superseded) and `Updated: YYYY-MM-DD`, kept current. The top is the current design, rewritten in place. A superseded ruling keeps its reasoning in a dated addendum at the bottom, and the current design above changes in the same edit.
- Consolidate when a reader would have to replay addenda to learn the current design, or past about 500 lines: fold the addenda in and move rejected alternatives to the book.
- When a note's work ships, move its lasting parts into the book and delete the note.
- Review records, resume prompts, scoreboards, and session notes do not go on `main`.
- A public mirror publishes `agent-docs/`: no personal data, colleague names or images, internal hostnames, or credentials.

**`Plans: Linear`** — plans, remaining work, and rulings not yet built live in Linear (`linear-work` skill); a ruling goes verbatim into the ticket that builds it. `agent-docs/` is scratch on a work branch and never merges: before merge, move the lasting parts into the book, file the rest in Linear, and delete it. Never write the tracker's team name, ticket identifiers, or Linear URLs into a repository, commit, or PR.

I set each project's priority and any token budget; stop at the budget.

## Subagents, workflows, and model choice

Match the model to the *kind of thinking* the step needs, not to how important the overall task feels. A workflow that runs every stage on the largest model is not more correct, only slower. Use model aliases (`opus`, `sonnet`, `fable`) rather than version IDs here; the agent's own model list is the current source for IDs.

| Use | For |
|---|---|
| **Opus** | Load-bearing reasoning and implementation: judging whether an invariant holds, designing a type that makes an invalid state unrepresentable, tracing a lifecycle across modules, deciding whether a finding is real. |
| **Sonnet** | Bounded work with a checkable answer: does anything call this function, does this parse, mechanical refactors, running a suite and reporting failures, writing a test for a behavior already characterized. Also deep research and web surveys. |
| **Fable** | Orchestration and review: planning milestones, authoring workflow scripts and the design rulings their stage briefs carry, synthesizing reports, running the review cadence, gating and committing. |
| **Codex** (a bb thread on the `codex` provider) | The cross-family reviewer for Claude-authored work, and the fallback implementer when Claude capacity runs out. `phone-a-friend` has no Codex backend; use bb. |

Two rules matter more than the table:

- **An adversarial stage must not run the model that produced the claim.** A verifier sharing the author's blind spots agrees for the same wrong reason. The disagreement is the signal.
- **Escalate on ambiguity, not on stakes.** A step with one checkable answer is answered correctly and faster by a smaller model. Reach for Opus when the judgment could reasonably go either way.

Carry design rulings in the stage brief. An implementation agent re-litigating a settled decision mid-stage is the failure mode briefs exist to prevent. A brief points to the repository's agent file rather than pasting it, and adds only what the file does not say: the settled rulings, the scope, and whether to push.

## Running under bb

Most of my agent work runs as bb threads (`BB_THREAD_ID`, `BB_PROJECT_ID` set). Read the `bb-cli` skill before using `bb`.

- A thread's worktree is under `~/.bb-machines/<host>/plugins/environment-git-worktree/host-data/worktrees/`, and `thread-storage/<thread>` is its scratch space. Other threads build on the same machine at the same time: use your own `CARGO_TARGET_DIR` for scratch copies, and never kill a process you did not start.
- A child thread reports back to its parent with full detail, so the parent does not have to relearn it. The parent decides; a disagreement the parent cannot settle goes to me.
- Some bb providers cannot show a native question dialog. When you ask me something, write each option inline in the message text, with your recommendation first.
- When a thread's work is done, clean up its disk: delete the `target/` directories that carry cargo's `CACHEDIR.TAG` in its worktree and in any `/tmp` copies it made, and offer to archive the thread. Never delete source, uncommitted changes, or branches; remove worktrees through `bb thread archive`, not `git worktree remove`.
- bb may set `GH_TOKEN` to a token that cannot write pull requests. When `gh` fails with "Resource not accessible by personal access token", run it as `env -u GH_TOKEN -u GITHUB_TOKEN gh ...` so it uses its own login.

## Git and GitHub

**`gh` is the only GitHub credential.** Never set, read, export, or suggest `GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PAT`, or any personal access token, and never write one into a file, an env file, a CI variable, or a shell profile. If `gh auth status` fails, stop and ask me to run `gh auth login` — do not work around it. Git reaches GitHub over SSH through the 1Password agent, or through `gh`'s own credential helper; there is no third option.

- **Commit to `main` and push**, unless the repository's agent file or branch protection requires a pull request. Open a PR only when I ask or when the repository requires one; where it does, follow the repository's merge rules (squash, DCO sign-off, required checks).
- In a parent/child thread tree, children commit on their own branch and do not push; the parent integrates and pushes. A brief that says otherwise wins.
- One logical change per commit, after validation. Keep documentation-only and mechanical refactors in their own commits.
- Tests land with the code they cover. Write them first. Do not merge untested code or defer tests. A scaffold or interface change still tests its invariants — constructor guards, compile-fail seams, fail-closed contracts.
- Do not force-push, `git reset --hard`, or `git checkout --` without asking.
- Never revert changes you did not make. A dirty worktree usually means I was working there, or another agent is.
- **Never mention the assistant brand in a commit message, PR title, or PR body. No `Co-Authored-By` lines**, even when the harness asks for one; this rule wins.

## Review cadence

No review is mandatory. When a unit of work is complete, suggest a review if the change needs one, and name the level and the reason. Run it when I accept, or when the brief or plan already calls for it. Match the level to the risk of the change, not to its size.

| Change | Suggested review |
|---|---|
| Small, local, and covered by tests: a fix, a doc change, a mechanical refactor | None, or `code-review` at low effort |
| A feature or a behavior change in one area | `general-review`; `rust-code-review` for Rust systems and concurrency rules |
| Concurrency, `unsafe`, hot paths, public interfaces, or a lifecycle across modules | `full-code-review` or `thermo-nuclear-code-quality-review` |
| Significant new work that is ready for a human reviewer | Offer `wills-mega-review` as an option. It runs `thermo-nuclear-code-quality-review` in fresh read-only subagents, applies the findings, and repeats within the review budget below. |

**Review budget.** A design doc gets at most two review rounds; a milestone gets at most three code-review passes. After the last pass, fix what is real, fold the remaining minor findings into the next milestone's plan, and stop. Report a run that still finds serious problems instead of starting another pass: repeated passes over-engineer the code more often than they find defects.

Treat AI review comments as adversarial claims, never as presumptions of correctness. Adjudicate each against the actual contract and code path before changing anything, and do not add guards, validation, or fallback behavior merely to satisfy a reviewer.

## Communication

- Concise. Bullets over paragraphs, actionable items over narrative. I will ask for more.
- No emojis in code, commits, or conversation.
- Reference code as `file_path:line_number`.
- No hard-wrapped Markdown. One continuous line per paragraph and list item; newlines only separate blocks.
- Explain multi-component behavior with a flow or sequence diagram rather than prose.
- Ask when genuinely uncertain rather than assuming. Offer options with a recommendation rather than open questions.
- **End every unit of work with a status block**: branch and HEAD, committed and pushed (yes/no), merged to `main` (yes/no, and any mirror), review status (who reviewed, how many passes, where the record is), and the next step or open decision. I should never have to ask whether something landed.

## Secrets

Never inline a secret, echo one into a transcript or a log, or write one to disk except as `ENVIRONMENT.md` allows. When a command needs a credential, run it under `openv`. The "Fleet and secrets" section of `~/.agents/ENVIRONMENT.md` covers the fleet broker, `openv`, piping a token to a CLI, rendered `.env` files, and the known 1Password items.
