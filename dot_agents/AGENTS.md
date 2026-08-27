# Agent Instructions — Ryan Olson

Canonical file. `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` are symlinks to it; the source of
truth is `dot_agents/AGENTS.md` in the chezmoi repo. Edit it there and run `chezmoi apply`.

A repository's own `AGENTS.md` / `CLAUDE.md` **overrides this file** wherever the two disagree.
This is the default, not the law.

## Who I am

Rust and Python systems engineer. Datacenter-scale distributed inference serving — Dynamo, rhino,
kvbm, velo, roundhouse. Performance engineering is the core competency, so treat performance as a
correctness property rather than a follow-up.

## Session start

1. Resolve the agent and its home. `CODEX_*` in the environment means Codex
   (`${CODEX_HOME:-~/.codex}`); `CLAUDECODE` means Claude Code (`${CLAUDE_HOME:-~/.claude}`). If it
   is ambiguous, ask rather than guess.
2. Read the repository's own agent file and treat it as authoritative over this one.
3. `git worktree list` — know the checkout layout before touching a build directory or a venv.

## How I work

- **Direct path first.** Make the smallest change that satisfies the request. Do not refactor
  working code in passing, and do not add abstraction for a second caller that does not exist yet.
- **Reproduce before you theorize.** No long explanations of what might be wrong. Get the failure
  in front of you first.
- **Show numbers.** Logs, metrics, benchmarks. "It is faster" is not a result.
- **Profile before guessing** when the question is performance.

## Validating a claim

**Write the test first, then rule, then fix.** This applies to review findings, bug reports,
hypotheses about behavior, and "I think X is broken" of any kind. A claim confirmed by reading code
is an opinion; a claim confirmed by a failing test is a fact, and the same test proves the fix.

1. **Write a test that fails** for the reason the claim says it should. If it passes, the claim is
   wrong — or the test does not exercise what the claim is about, which is worth knowing first.
2. **If it cannot be tested, make it testable.** A defect no test can reach usually means the seam
   is in the wrong place. Add the accessor, extract the pure function, split the type. That change
   is part of the fix, not a detour. Prefer additive, behavior-preserving changes so the failing
   test is unambiguously about the defect.
3. **Rule on the claim**: *valid*, *partially valid* (the defect is real but the described mechanism
   is wrong — spell out the correction), or *invalid*. An external reviewer being mistaken is an
   ordinary outcome.
4. **Only then fix it**, and keep the test.

The order is about evidence, not about stopping — validating and fixing in one pass is the normal
case. Ask only when the finding turns out to be a design question, the fix is far larger than the
defect, or two valid remedies point different ways.

Where validation genuinely lands before the fix, mark failing assertions
`#[ignore = "<finding>: <why it fails>"]` rather than leaving the suite red or deleting the
evidence, and keep passing control tests live — they are what prove the failing ones are not
tautological. But be honest: **an ignored test enforces nothing.** Removing the ignore is the first
step of the fix, not a cleanup afterwards.

Fixing the described mechanism rather than the actual one leaves the defect in place behind a
passing test. That is worse than not having looked.

## Bounded test runs

Always run suites under a coreutils `timeout`: `timeout 900` for a full workspace suite, `timeout
300` for a targeted one. The reason is not slow tests — a hung test hangs the entire runner,
silently, and the sessions most likely to hang one are exactly the adversarial sessions that mutate
timeout and deadline code on purpose. Break a timeout path and its guard does not go red, it waits
forever.

On exit 124, suspect the newest test or the mutation just applied, and re-run the suspect binary
with `--test-threads=1 --nocapture` under a short timeout to name the hang.

## Compatibility policy

**While every caller lives inside the workspace**, the compiler can find all of them, so write no
code that exists to keep an old caller working. Change an interface and delete the old form; change
the callers. No second entry point, forwarding wrapper, re-export, or type alias for the old name.

Never name a thing `legacy`, `old`, `v1`, `compat`, `deprecated`, or `_unused`. Such a name asserts
that two forms exist when only one must. Delete the thing, or delete the prefix and merge it with
the live form. Two spellings of one rule drift apart — that is how a `legacy` shard derivation and
a `serving` one came to disagree on an expert-count check.

Keep code a design document names as deliberately unwired, and say where that document is.

**Once a crate is published or has external consumers this inverts, and semver discipline wins.**
Breaking changes need a version bump; `=` pins are load-bearing and must not be relaxed to carets;
new trait methods need default implementations, because a bare one breaks every out-of-tree impl;
removing a public item is a major bump. When in doubt about which regime applies, check whether
anything outside the workspace can name the symbol.

## Documentation

**Every PR leaves the documentation it touches consistent with the code.** Before calling a change
done, check the README and module docs, doc comments, examples, file trees, and build/run commands
against what you actually changed. Remove orphaned references to symbols you deleted or renamed
rather than leaving them. A symbol used in docs must be defined before first use.

Separately and periodically, run the `docs-sweep` skill across `agent-docs/`, code comments, and
user-facing documentation. That sweep is the only thing that catches drift no single PR owned.

Comments explain *why* a decision was made and what the alternative would have cost. A comment that
restates the code is noise; a comment that records the failure mode a design avoids is why the next
person does not undo it. History belongs in `git`, not in comments.

## `agent-docs/`

Plans, design rulings, and deep-dive evidence go in `agent-docs/`, not at the repository root.
README and module docs describe the code as it is; `agent-docs/` records where it is going and why.
Supersede a ruling with a dated addendum rather than a silent rewrite — the reasoning that was
overtaken is often the most useful part.

## Subagents and workflows

Match the model to the *kind of thinking* the step needs, not to how important the overall task
feels. A workflow that runs every stage on the largest model is not more correct, only slower.

| Use | For |
|---|---|
| **Opus** (`claude-opus-5`) | Load-bearing reasoning: judging whether an invariant holds, designing a type that makes an invalid state unrepresentable, tracing a lifecycle across modules, deciding whether a finding is real. Anything where being wrong is expensive and the answer is not lookup-shaped. |
| **Sonnet** (`claude-sonnet-5`) | Bounded work with a checkable answer: does anything call this function, does this parse, mechanical refactors, running a suite and reporting failures, writing a test for a behavior already characterized. |
| **Fable** (`claude-fable-5`) | Orchestrator, never the workhorse: planning milestones, authoring workflow scripts and the design rulings their stage briefs carry, synthesizing reports into documents, running the review cadence, gating and committing. |

Two rules matter more than the table:

- **An adversarial stage must not run the model that produced the claim.** A verifier sharing the
  author's blind spots agrees for the same wrong reason. The disagreement is the signal.
- **Escalate on ambiguity, not on stakes.** A step with one checkable answer is answered correctly
  and faster by a smaller model. Reach for Opus when the judgment could reasonably go either way.

Carry design rulings in the stage brief. An implementation agent re-litigating a settled decision
mid-stage is the failure mode briefs exist to prevent.

## Git and GitHub

**`gh` is the only GitHub credential.** Never set, read, export, or suggest `GITHUB_TOKEN`,
`GH_TOKEN`, `GITHUB_PAT`, or any personal access token, and never write one into a file, an env
file, a CI variable, or a shell profile. If `gh auth status` fails, stop and ask me to run
`gh auth login` — do not work around it. Git reaches GitHub over SSH through the 1Password agent,
or through `gh`'s own credential helper; there is no third option.

- Draft PRs first for non-trivial changes.
- One reviewable concern per PR. Keep documentation-only and mechanical refactors in their own
  small PRs. Each milestone or phase ships as its own PR, branched from current `main`.
- Tests land with the code they cover. Write them first. Do not merge untested code or defer tests.
  A scaffold or interface PR still tests its invariants — constructor guards, compile-fail seams,
  fail-closed contracts.
- Commit locally as work progresses: one logical change per commit, after validation.
- Do not force-push, `git reset --hard`, or `git checkout --` without asking.
- Never revert changes you did not make. A dirty worktree usually means I was working there.
- **Never mention the assistant brand in a commit message, PR title, or PR body. No
  `Co-Authored-By` lines.**

## Review cadence

Before asking for human review, run `wills-mega-review`: it drives
`thermo-nuclear-code-quality-review` in a fresh read-only subagent, applies the findings, and
repeats until a clean pass. Use `full-code-review` when a combined general and deep review is
wanted in one shot, and `rust-code-review` for Rust-specific systems and concurrency rules.

Treat AI review comments as adversarial claims, never as presumptions of correctness. Adjudicate
each against the actual contract and code path before changing anything, and do not add guards,
validation, or fallback behavior merely to satisfy a reviewer.

## Communication

- Concise. Bullets over paragraphs, actionable items over narrative. I will ask for more.
- No emojis in code, commits, or conversation.
- Reference code as `file_path:line_number`.
- No hard-wrapped Markdown. One continuous line per paragraph and list item; newlines only separate
  blocks.
- Explain multi-component behavior with a flow or sequence diagram rather than prose.
- Ask when genuinely uncertain rather than assuming. Prefer offering options over open questions.
- Use the `simple-english` skill for documentation, READMEs, PR descriptions, issue descriptions,
  and commit messages. Not for conversation.

## Secrets

Secrets live in 1Password and are injected on demand: `openv <cmd>` runs a command under
`op run --env-file ~/.config/dynamo/dev.env.op`. Never inline a secret, never write one to disk,
and never echo one into a transcript or a log. If a command needs a credential, wrap it in `openv`
rather than exporting the value into the shell.
