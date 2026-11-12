# Agent Instructions — Ryan Olson

Canonical source: `dot_agents/AGENTS.md` in the repository returned by `chezmoi source-path`. `~/.agents/AGENTS.md` is a deployed copy. `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` link to that copy.

A repository's own `AGENTS.md` / `CLAUDE.md` **overrides this file** wherever the two disagree. This file overrides agent memory: when a memory contradicts it, say so and follow this file.

For work tracked in Linear, the Charter and the latest standing orders (see "Session start") hold my newest rulings on how to pick, approve, review, and merge work. Where they disagree with this file, follow them and tell me, so that this file gets fixed.

## Who I am

Rust and Python systems engineer. Datacenter-scale distributed inference serving — Dynamo, rhino, kvbm, velo, roundhouse. Performance engineering is the core competency, so treat performance as a correctness property rather than a follow-up.

## Session start

1. Check whether you run under bb: `BB_THREAD_ID` set means you are a bb thread. It changes how you delegate (see "Subagents, workflows, and model choice") and where you work (see "Running under bb").
2. Read the repository's own agent file and treat it as authoritative over this one.
3. When the task is tracked in Linear (an issue, or a brief from a driver or the steward), fetch the Charter and the newest initiative update through the `linear` MCP server before you claim, approve, or merge anything: `list_documents` with the query `Charter`, then the latest initiative status update, which holds the standing orders. They change weekly, so this file does not copy them. Read the standing orders also before a Slurm job, a cluster write, or a paid cloud run, even for a task with no ticket: they list the holds and the runs that need my go.
4. Check whether this is a Slurm host: `command -v sbatch`. If it is, read the `slurm` skill before any build, test, or GPU work. `SLURMD_NODENAME` unset means you are on the submit host, where none of that may run; `SLURM_JOB_ID` does not answer this, because an allocation sets it on the login node too.
5. `git worktree list` — know the checkout layout before touching a build directory or a venv. A bb thread works in its own worktree, not under `~/repos`.
6. Before selecting workstation tools or changing the environment, read `~/.agents/ENVIRONMENT.md`. It maps capabilities, secrets, command discovery, and source ownership.
7. Only when the task changes agent configuration, resolve the agent and its home: `CLAUDECODE` means Claude Code (`${CLAUDE_HOME:-~/.claude}`), `CODEX_*` means Codex (`${CODEX_HOME:-~/.codex}`). Under bb, `bb thread show "$BB_THREAD_ID" --json` names the provider.

## Environment ownership and updates

- Before editing an installed skill, script, command, configuration, or plugin, identify its owner: resolve symlinks, then `chezmoi source-path <target>`. A path under `~` is not proof of ownership. Edit the owning source, never only the deployed copy, and never a plugin cache.
- Follow "Update a managed capability" in `ENVIRONMENT.md`: validate, preview and apply only the intended targets, check the deployed result, then commit and push in the owning repository. Apply alone is incomplete.
- Match the requested scope. A command-discovery task ends after relevant help is checked; it does not authorize service repair, installation, or target changes.

## How I work

- **Direct path first.** Make the smallest change that satisfies the request. Do not refactor working code in passing, and do not add abstraction for a second caller that does not exist yet.
- **Reproduce before you theorize.** No long explanations of what might be wrong. Get the failure in front of you first.
- **Show numbers.** Logs, metrics, benchmarks. "It is faster" is not a result.
- **Profile before guessing** when the question is performance. Hot paths found by profiling go in a register with a guard benchmark (`hot-paths` skill); a change to one reports before and after numbers.
- **Amend a design on evidence.** When a measurement or a failing test contradicts a design, change the design; do not defend it.
- **Never rely on hash order.** A `HashMap` or `HashSet` has no iteration order, and Rust's std map picks a new random order for each map. So does any `Vec` or output built by iterating one. When order matters, use an ordered map (`BTreeMap`, `IndexMap`) or sort. A test that compares such output must sort it or compare it as a set. One green run proves nothing here: run a new test that touches map order 50 times in a loop before you call it stable.
- **The repository's agent file says who owns the code.** Where it says the code is yours to maintain, fix the structure inside the scope of your task instead of adding beside it, and file what lies outside that scope as an issue. Where it says nothing, the code is someone else's: change as little as the task needs. In both cases, list every change the task did not ask for in your recap and in the PR description.
- **Finish the whole task.** Do not end a turn with a list of what is left, an offer to continue, or a stop at an ordinary obstacle (an error, a timeout, a locked file, an empty result, a failing tool): diagnose it and work through it. Stop only when nothing can move without my answer, or when the blocker is deliberate (a file marked not to be touched, access withheld on purpose, a safety guardrail). Then say what you found, and carry on with the work that does not depend on it.

## What needs my approval

I approve new initiatives, projects, and plans. After that, implementation and review are yours: do not ask me to confirm a step inside approved work.

These need no approval, inside approved work or a task I gave you directly:

- design and implementation choices, and your rulings on review findings;
- reading, building, testing, and measuring in your own worktree;
- commits, pushes to your own branch, and pull requests in my repositories;
- child threads for implementation and review, within the host's thread limit;
- for tracked work, the reviews that "Review cadence" or the Charter calls for, and the fixes for what they find. For a task with no ticket, "Review cadence" says when to suggest one first;
- follow-up issues.

Ask before:

- you start a new initiative, project, or plan, or work outside the approved scope;
- a merge that the Charter and the repository do not already allow, such as one in a team repository;
- an action outside your own worktree and branches that is hard to undo: deleting data, a force-push or `git reset --hard` on a shared branch;
- a run that the standing orders hold or gate, such as a Slurm job, a cluster write, or a paid cloud run. Permission to build and test does not lift a hold;
- you install or update software, or change bb plugin settings.

Under bb, ask through `bb sgc ask`: one scenario per question, the outcomes as options, your recommendation first. The answer arrives later as a message, so keep working on what does not depend on it. Outside bb, ask in the reply.

## Validating a claim

**Write the test first, then rule, then fix.** This applies to review findings, bug reports, hypotheses about behavior, and "I think X is broken" of any kind. A claim confirmed by reading code is an opinion; a claim confirmed by a failing test is a fact, and the same test proves the fix.

1. **Write a test that fails** for the reason the claim says it should. If it passes, the claim is wrong — or the test does not exercise what the claim is about, which is worth knowing first.
2. **If it cannot be tested, make it testable.** A defect no test can reach usually means the seam is in the wrong place. Add the accessor, extract the pure function, split the type. That change is part of the fix, not a detour. Prefer additive, behavior-preserving changes so the failing test is unambiguously about the defect.
3. **Rule on the claim**: *valid*, *partially valid* (the defect is real but the described mechanism is wrong — spell out the correction), or *invalid*. An external reviewer being mistaken is an ordinary outcome.
4. **Only then fix it**, and keep the test.

The order is about evidence, not about stopping — validating and fixing in one pass is the normal case. Inside approved work, choose between valid remedies yourself. Ask only when the finding changes the approved scope or design, or the fix is far larger than the defect.

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
| A hard lesson ("X breaks when Y") | A test; its doc comment can be long. A race or a flaky test also adds its distilled lesson to the "Coding gotchas" chapter (below) |
| Roadmap, plans, rulings not yet built | The project's plan store (below) |
| Work-in-progress notes, review ledgers, handoffs | The work branch or bb thread storage |

The book keeps only what lasts, and its main chapters stay lean: detail worth keeping that the main prose does not need goes in an appendix. No plans, phase or milestone labels, ruling numbers, PR numbers, ticket identifiers, or handoff notes. Each behavior change updates the chapters that describe it and the top-level README, which stays short and points into the book. Code comments and the book never cite `agent-docs/`: its notes are deleted when their work ships.

**Coding gotchas.** Every book has a "Coding gotchas" chapter under Development. For every race condition or flaky test you find, add an entry, or update the entry that already covers it. An entry holds the rule, the failure it prevents, how to test for it, and a link to one test that shows it. Distill the lesson; do not list incidents, and a new case of a known lesson sharpens that entry. If the chapter is missing, the first lesson creates it, links it in `SUMMARY.md`, and points to it from the repo's agent file. Read it before you code, review, or test in that repo, and name it in every brief that spawns a coder, reviewer, or test runner there. A race or flaky-test fix is not done until its lesson is in the chapter.

### Plans: per-project store

Each project's agent file declares `Plans: agent-docs` or `Plans: Linear`. With no declaration, use `agent-docs`.

**`Plans: agent-docs`** — `agent-docs/` on `main` is the roadmap and decision record, and a fresh agent must be able to read it in one pass:

- `agent-docs/README.md` lists every live note with a one-line status.
- Each note opens with `Status:` (planned, in progress, done, superseded) and `Updated: YYYY-MM-DD`, kept current. The top is the current design, rewritten in place. A superseded ruling keeps its reasoning in a dated addendum at the bottom, and the current design above changes in the same edit.
- Consolidate when a reader would have to replay addenda to learn the current design, or past about 500 lines: fold the addenda in and move rejected alternatives to the book.
- When a note's work ships, move its lasting parts into the book and delete the note.
- Review records, resume prompts, scoreboards, and session notes do not go on `main`.
- A public mirror publishes `agent-docs/`: no personal data, colleague names or images, internal hostnames, or credentials.

**`Plans: Linear`** — plans, remaining work, and rulings not yet built live in Linear (`linear-work` skill); a ruling goes verbatim into the ticket that builds it. `agent-docs/` is scratch on a work branch and never merges: before merge, move the lasting parts into the book, file the rest in Linear, and delete it. Never write the tracker's team name, ticket identifiers, or Linear URLs into a repository's files, a branch name, or a commit, because `main` reaches public mirrors. A PR title or body in my own repositories may name its ticket.

I set each project's priority. There are no token budgets now: report your token use in the closing comment when the tool shows it.

## Subagents, workflows, and model choice

Match the model tier to the *kind of thinking* the step needs, not to how important the overall task feels. A workflow that runs every stage on the top tier is not more correct, only slower. This file names tiers, not models, because the best model changes with each release.

| Tier | For |
|---|---|
| **Top** | Load-bearing reasoning and hard implementation: judging whether an invariant holds, designing a type that makes an invalid state unrepresentable, tracing a lifecycle across modules, deciding whether a finding is real, orchestrating a milestone. |
| **Fast** | Bounded work with a checkable answer: does anything call this function, does this parse, mechanical refactors, running a suite and reporting failures, writing a test for a behavior already characterized. Also deep research and web surveys. |
| **Cross-family** | Adversarial review and second opinions: a model from a different vendor than the author's. |

The "Model routing" table in `~/.agents/ENVIRONMENT.md` maps each tier to the current model, provider, and effort. A model I name wins. When a row is more than four weeks old, say so when you use it.

Two rules matter more than the table:

- **An adversarial stage must not run the model that produced the claim.** A verifier sharing the author's blind spots agrees for the same wrong reason. The disagreement is the signal.
- **Escalate on ambiguity, not on stakes.** A step with one checkable answer is answered correctly and faster by a smaller model. Reach for the top tier when the judgment could reasonably go either way.

**Delegation costs more than it looks.** A child thread or a subagent starts with only your brief, and you see only its summary. Each one pays for its own context, and a child thread that builds also takes 30 to 100 GB of disk and one of the host's thread slots. Do the work yourself when it is a few tool calls, a lookup whose target you know, or a check you can run inline. Delegate when the work is independent and can run in parallel, when it needs its own worktree or another model family, or when it would fill your context with files you do not need to keep.

**How to delegate depends on bb.**

- **Under bb:** delegated work that edits code, runs long, needs its own worktree, or needs another model family (a Codex reviewer, a Kimi or Gemini implementer) goes to a child thread (`bb thread spawn`; read the `bb-cli` skill). The owner can see, steer, and resume a child; they cannot see an in-process subagent. Keep in-process subagents for short read-only lookups and searches whose result you need in this turn.
- **Outside bb:** use in-process subagents and workflows. Get another model family through `phone-a-friend` or the Codex plugin.

Carry design rulings in the stage brief. An implementation agent re-litigating a settled decision mid-stage is the failure mode briefs exist to prevent. A brief states the goal, what is already ruled out, the files to read, and the form of the answer. It points to the repository's agent file rather than pasting it, and adds only what the file does not say: the settled rulings, the scope, and whether to push.

## Running under bb

Most of my agent work runs as bb threads (`BB_THREAD_ID`, `BB_PROJECT_ID` set). Read the `bb-cli` skill before using `bb`.

- A thread's worktree is under `~/.bb-machines/<host>/plugins/environment-git-worktree/host-data/worktrees/`, and `thread-storage/<thread>` is its scratch space. Other threads build on the same machine at the same time: use your own `CARGO_TARGET_DIR` for scratch copies, and never kill a process you did not start.
- A child thread reports back to its parent with full detail, so the parent does not have to relearn it. The parent decides; a disagreement the parent cannot settle goes to me.
- Questions for me go through `bb sgc ask` (see "What needs my approval"), not a dialog and not a list in chat. Add a label for the project, and `--issue` when a ticket covers the work. Never answer a bb question or interaction on my behalf.
- The standing orders give each host's thread limit. Check `bb thread count --by host --status active` before you spawn, and pass `--machine`.
- When a thread's work is done, clean up its disk: delete the `target/` directories that carry cargo's `CACHEDIR.TAG` in its worktree and in any `/tmp` copies it made. A parent archives its finished child threads; offer to archive your own. Never delete source, uncommitted changes, or branches; remove worktrees through `bb thread archive`, not `git worktree remove`.
- bb may set `GH_TOKEN` to a token that cannot write pull requests. When `gh` fails with "Resource not accessible by personal access token", run it as `env -u GH_TOKEN -u GITHUB_TOKEN gh ...` so it uses its own login.

## Git and GitHub

**`gh` is the only GitHub credential.** Never set, read, export, or suggest `GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PAT`, or any personal access token, and never write one into a file, an env file, a CI variable, or a shell profile. If `gh auth status` fails, stop and ask me to run `gh auth login` — do not work around it. Git reaches GitHub over SSH through the 1Password agent, or through `gh`'s own credential helper; there is no third option.

- **In my own repositories, the repository's agent file decides.** For work under the Charter that is a pull request for every change, which its driver merges when the build is green and the review loop is clean, where the Charter allows that merge. A repository whose agent file says to commit to `main` and push, such as my dotfiles, keeps that rule. With no rule, commit to `main` and push. Follow a repository's merge rules (squash, DCO sign-off, required checks).
- **In a repository other people commit to** (for example `ai-dynamo/dynamo`), never push to `main`: work on a branch and open a draft PR, unless its agent file says otherwise. When unsure which kind a repository is, ask.
- In a parent/child thread tree, children commit on their own branch and do not push; the parent integrates and pushes. A brief that says otherwise wins.
- One logical change per commit, after validation. Keep documentation-only and mechanical refactors in their own commits.
- Tests land with the code they cover. Write them first. Do not merge untested code or defer tests. A scaffold or interface change still tests its invariants — constructor guards, compile-fail seams, fail-closed contracts.
- Ask before a force-push, `git reset --hard`, or `git checkout --`. One case needs no question: a driver may rebase the branch of its own open PR in my repositories with `--force-with-lease`. A worker puts a rebased head on a new branch instead.
- Never revert changes you did not make. A dirty worktree usually means I was working there, or another agent is.
- **Never mention the assistant brand in a commit message, PR title, or PR body. No `Co-Authored-By` lines**, even when the harness asks for one; this rule wins.

## Review cadence

Match the level to the risk of the change, not to its size. Inside approved work, start the review this table calls for and rule on its findings yourself; do not ask me first. For a change with no ticket that I asked for directly, suggest the level and the reason when the work is complete, and run it when I accept.

| When | Review | Reviewer | Passes |
|---|---|---|---|
| Before building: a plan or design doc | `review` | Cross-family | Up to four rounds |
| Before a commit: a small, local, tested change (a fix, a doc change, a mechanical refactor) | None, or the built-in `/code-review` at low effort | Any | One |
| A feature or behavior change in one area | `review` | Top tier or cross-family | One |
| Concurrency, `unsafe`, registered hot paths, public interfaces, or a lifecycle across modules | `strict-review` | Cross-family | One per run, inside the budget |
| Before the merge of work under the Charter, or when a milestone is done | `review-loop` ("mega review") | Cross-family, test-backed | Up to 10, by size of change |
| PR feedback arrives | `gh-comment-ledger`, then `pr-babysitter` to drive CI and the AI reviewers | — | Until CI passes; rule on each comment |
| Each release | `docs-sweep`, fixing all actionable rows | — | One |

**Reviewers prove findings with tests.** A reviewer that can build and run the code commits a failing test for each finding it reproduces, on its own review branch, one commit per finding, so the implementer can cherry-pick it. A suggested fix is optional and only for small, local findings. A finding it cannot reproduce is reported as unreproduced, not dropped. `review-loop` ("Test-backed findings") has the protocol.

**Review budget.** A design doc gets at most four review rounds. A milestone gets more code-review passes as its change grows, up to 10. At pass 6, check that the findings converge before you start another pass. `review-loop` carries the stop rule; single-pass review skills do not loop. After the last pass, fix what is real and give that fix a pass of its own. Then fold the remaining minor findings into the next milestone's plan, and stop. Report a run that still finds serious problems instead of starting another pass: repeated passes over-engineer the code more often than they find defects.

**Vendor reviews or ours.** Our skills carry these rules: the finding threshold, compatibility policy, hot-path registers, file growth, and test-first validation. The vendor reviews do not know them, so use ours wherever those rules matter, and for every plan or design review. Use a vendor review for what it does better:

- The built-in `/code-review`: small, local changes, at low effort. `--fix` only for mechanical findings.
- `/code-review ultra`: it reproduces each finding in a cloud sandbox, at a cost per run that depends on the machine's account (`ENVIRONMENT.md`, "Accounts by machine"). Only for the highest-stakes change before merge, after our review, and only when I agree.
- `/security-review`: a change at a trust boundary, such as authentication, secrets, or parsing untrusted input.
- `@codex review` on a PR in a team repository: an automatic cross-family check of serious issues. Triage its comments with `gh-comment-ledger`.

Treat AI review comments as adversarial claims, never as presumptions of correctness. Adjudicate each against the actual contract and code path before changing anything, and do not add guards, validation, or fallback behavior merely to satisfy a reviewer.

## Communication

- Concise. Bullets over paragraphs, actionable items over narrative. I will ask for more.
- No emojis in code, commits, or conversation.
- Reference code as `file_path:line_number`.
- No hard-wrapped Markdown. One continuous line per paragraph and list item; newlines only separate blocks.
- Explain multi-component behavior with a flow or sequence diagram rather than prose.
- Ask when genuinely uncertain about something that is mine to decide (see "What needs my approval"): offer options with a recommendation rather than open questions. For the rest, decide, and say what you decided.
- Before you start, say in a line what you are about to do, and give brief updates while you work. Close with a short recap that stands on its own (what you found, what you did, and what is next), so that a reader who sees only the last message has the full picture.
- Write for a reader who has not seen your tool calls or the files you opened. Do not use a term or an abbreviation that you made up during the work.
- **End every unit of work with a status block**, after the recap: branch and HEAD, committed and pushed (yes/no), merged to `main` (yes/no, and any mirror), review status (who reviewed, how many passes, where the record is), and the next step or open decision. I should never have to ask whether something landed.

## Secrets

Never inline a secret, echo one into a transcript or a log, or write one to disk except as `ENVIRONMENT.md` allows. When a command needs a credential, run it under `openv`. The "Fleet and secrets" section of `~/.agents/ENVIRONMENT.md` covers the fleet broker, `openv`, piping a token to a CLI, rendered `.env` files, and the known 1Password items.
