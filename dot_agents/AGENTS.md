# Agent Instructions — Ryan Olson

Canonical source: `dot_agents/AGENTS.md` in the repository returned by `chezmoi source-path`. `~/.agents/AGENTS.md` is a deployed copy. `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` link to that copy.

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
4. Read `~/.agents/ENVIRONMENT.md` once per session before selecting workstation tools or planning environment changes. It maps capabilities, command discovery, prerequisites, and source ownership. If it is missing, report the gap and inspect the chezmoi source without applying unrelated files.

## Environment ownership and updates

- Use the established tooling described in the environment guide. Check availability in the actual execution shell and host. Read the relevant skill and current command help before use.
- Before editing an installed skill, script, command, configuration, or plugin, identify its owner. Resolve symlinks before mapping deployed files with `chezmoi source-path <target>`. A path under `~` is not proof of ownership.
- For chezmoi-managed files, edit the repository source, including templates and executable attributes. Never leave the only change in a deployed copy.
- For files owned by another project or plugin manager, update that source and use its installation procedure. Do not edit plugin caches or adopt application state into chezmoi.
- Validate the source change, preview and apply only the intended targets, then check the deployed result. Preserve unrelated source changes and deployment drift.
- Finish an authorized environment update with a commit and push to the working branch in its owning repository. Follow the draft-PR policy for non-trivial changes. Apply alone is incomplete; report any validation, deployment, commit, or push blocker.
- Match the requested scope. A command-discovery task ends after relevant help is checked; it does not authorize service repair, installation, or target changes.

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

Separately and periodically, run the `docs-sweep` skill across the book, code comments, and
user-facing documentation. That sweep is the only thing that catches drift no single PR owned.

Comments explain *why* a decision was made and what the alternative would have cost. A comment that
restates the code is noise; a comment that records the failure mode a design avoids is why the next
person does not undo it. History belongs in `git`, not in comments.

### The book

Every Rust project past its first weeks has an mdBook in `docs/` (`docs/book.toml`, chapters in
`docs/src/`). `../velo/docs` is the model: its sections (Introduction, Getting started, Concepts,
Guides, Operations, Development, Appendix), its `book.toml` (`create-missing = false`, mermaid,
linkcheck), `scripts/build-book.sh`, and its `development/documentation.md`, which each book copies
and adapts.

| Text | Place |
|---|---|
| How a subsystem works, and why, with the rejected alternative | A chapter in the book |
| Measured numbers, with the command and setup that produced them | A chapter under Operations |
| Rejected designs and negative results | A chapter under Development |
| The reason for a line of code | A short code comment |
| A hard lesson ("X breaks when Y") | A test; its doc comment can be long |
| Plans, remaining work, follow-ups, reports about the work | Linear (`linear-work` skill) |
| Notes while work is in progress | `agent-docs/` on a work branch |

- **The book keeps only what lasts**: how it works, why, numbers with their setup, negative
  results. No plans, phase or milestone labels, ruling numbers, PR numbers, ticket identifiers, or
  handoff notes. History belongs in `git`.
- **Each PR that changes behavior updates the chapters that describe it** and the top-level README.
  The README stays short and points into the book.
- Write the book, READMEs, and comments with the `simple-english` skill.

### `agent-docs/`

`agent-docs/` is scratch space on a work branch, and **it never merges to `main`**. Before the
branch merges, move the lasting parts into the book, file the remaining work in Linear, and delete
the directory from the branch. A new project may keep `agent-docs/` on `main` for its first weeks,
before it has a book; distill it into the book and Linear as soon as the design settles.

A design ruling that is not built yet goes, verbatim, into the body of the Linear ticket that builds
it. Once it is built, its reason and the alternative it rejected go into the book chapter, without
its number. Supersede a ruling in progress with a dated addendum rather than a silent rewrite — the
reasoning that was overtaken is often the most useful part, and the book's Development section is
where it lands.

## Subagents and workflows

Match the model to the *kind of thinking* the step needs, not to how important the overall task
feels. A workflow that runs every stage on the largest model is not more correct, only slower.

| Use | For |
|---|---|
| **Opus** (`claude-opus-5`) | Load-bearing reasoning: judging whether an invariant holds, designing a type that makes an invalid state unrepresentable, tracing a lifecycle across modules, deciding whether a finding is real. Anything where being wrong is expensive and the answer is not lookup-shaped. |
| **Sonnet** (the newest Sonnet, `sonnet` alias; 5.5 as of 2026-09-29) | Bounded work with a checkable answer: does anything call this function, does this parse, mechanical refactors, running a suite and reporting failures, writing a test for a behavior already characterized. |
| **Fable** (`claude-fable-5`) | Orchestrator, never the workhorse: planning milestones, authoring workflow scripts and the design rulings their stage briefs carry, synthesizing reports into documents, running the review cadence, gating and committing. |

Two rules matter more than the table:

- **An adversarial stage must not run the model that produced the claim.** A verifier sharing the
  author's blind spots agrees for the same wrong reason. The disagreement is the signal.
- **Escalate on ambiguity, not on stakes.** A step with one checkable answer is answered correctly
  and faster by a smaller model. Reach for Opus when the judgment could reasonably go either way.

Carry design rulings in the stage brief. An implementation agent re-litigating a settled decision
mid-stage is the failure mode briefs exist to prevent.

## Work tracking

Work items live in Linear: plans, remaining work, PR and review follow-ups, and the queue for agent
swarms. Follow the `linear-work` skill for every ticket you create, claim, or close. I set each
project's and ticket's priority and its token budget (the estimate, in millions of tokens); agents
work inside them and stop at the budget. Never write the tracker's team name, ticket identifiers,
or Linear URLs into a repository, commit, or PR.

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

No review is mandatory. When a unit of work is complete, suggest a review if the change needs one, and name the level and the reason. Run it when I accept. Match the level to the risk of the change, not to its size.

| Change | Suggested review |
|---|---|
| Small, local, and covered by tests: a fix, a doc change, a mechanical refactor | None, or `code-review` at low effort |
| A feature or a behavior change in one area | `general-review`; `rust-code-review` for Rust systems and concurrency rules |
| Concurrency, `unsafe`, hot paths, public interfaces, or a lifecycle across modules | `full-code-review` or `thermo-nuclear-code-quality-review` |
| Significant new work that is ready for a human reviewer | Offer `wills-mega-review` as an option. It runs `thermo-nuclear-code-quality-review` in fresh read-only subagents, applies the findings, and repeats until a clean pass. |

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

## Fleet

`fleet` enrolls my machines with my own CA. On an enrolled machine (`fleet status` succeeds), a broker serves secrets, SSH signing, and tunnels without 1Password. Read `fleet --help` before use.

## Secrets

Secrets live in 1Password and are injected on demand: `openv <cmd>` runs a command with the
variables in `~/.config/dynamo/dev.env.fleet`, from the fleet broker first and from
`op run --env-file` when the broker fails. Never inline a secret, never echo one into a
transcript or a log, and never write one to disk except as below. If a command needs a credential,
wrap it in `openv` rather than exporting the value into the shell.

**A command that prints a secret.** When a doc says to pipe in a token (for example `<command that prints your token> | tool login --stdin`), pipe `fleet secret get "<op ref>"`. If fleet returns 404 and `op` is installed, pipe `op read "<op ref>"`. Take the reference from the known items below. If none fits, find candidates with `op item list` and ask me which one to use.

**The one on-disk exception: a rendered `.env`.** A tool that must run without prompts (an MCP
server, a daemon) may read a `.env` that is gitignored, mode 600, and generated from a mapping of
`op://` references kept outside the repo, at the path the repo names (else
`~/.config/<repo>/<name>.env.tpl`; wcep-experiments uses `~/.config/wcep/mcp.env.tpl`): the repo's
`.env.example` with each secret value replaced by `{{ op://<vault>/<item>/<field> }}`. Render with
the repo's render script if it has one, else `op inject -i <mapping> -o <.env>` and `chmod 600`.
Because 1Password is my store, build and maintain the mapping yourself rather than asking me for
values: find items by title (`op item list`) and fields by label (`op item get <item> --format
json`), never print a value, and ask only when the right item is ambiguous. Known items:

- NGC personal key, NIM Operations (`goirlvsxnepa`): `op://Development/NGC NIM Operations PAT/credential`.
  An `nvapi-` key; works for the API gateway and the `ngc` CLI in that org.
  The `Fleet` vault has a copy: `op://Fleet/NGC NIM Operations PAT/credential`.
- GitLab PAT for `gitlab-master.nvidia.com`: `op://Fleet/NV Gitlab PAT/credential`. Log in with
  `<command that prints the secret> | glab auth login --hostname gitlab-master.nvidia.com
  --git-protocol ssh --stdin`.
- Every fleet machine can read any `op://Fleet/...` reference through `fleet secret get`, with
  no broker policy entry (fleet-config `ae76cc5`, deployed 2026-10-21).
- `Development/NGC` and `Corp/NGC` are legacy NGC keys: the API gateway rejects them (401).
