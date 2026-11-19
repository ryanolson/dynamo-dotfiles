# Ryan Olson's agent instructions

Repository instructions override this guide. For tracked work, the Linear Charter and current standing orders take precedence. This guide overrides memory. Flag conflicts.

## Scope

Finish the requested work. Work through ordinary failures. Ask about decisions that change scope or need approval. Keep working on independent steps.

Improve code within scope when I own it. Elsewhere, make the smallest complete change. Use the task's acceptance criteria and verify them with relevant evidence. Limit tool output to what the task needs. Delegate only when parallel work, isolation, or independent judgment warrants it.

Routine work within scope needs no approval. Ask before new plans or scope changes, irreversible actions, unauthorized merges, gated runs, software installs or updates, and BB plugin changes. Under BB, use `bb sgc ask`.

## Before acting

- For tracked work, use `linear-work` and read the Charter and latest standing orders before claiming, approving, or merging. Read the standing orders before any Slurm job, cluster write, or paid cloud run.
- Check the checkout before builds. Under BB, use your thread's worktree for implementation. Read `bb-cli` before using `bb`.
- On a host with `sbatch`, read `slurm` before building, testing, or using GPUs. Run this work through Slurm when `SLURMD_NODENAME` is unset.
- Use [~/.agents/ENVIRONMENT.md](ENVIRONMENT.md) for tools, credentials, and managed files. Edit the owning source and verify the scoped deployment. Keep secrets out of logs and files except as the guide allows. For GitHub, use `gh` or the configured Git credential path.

## Engineering

- Reproduce defects with a failing test. Rule on the claim, fix the cause, and keep the test. Build tests first and run them under `timeout`.
- Treat performance as correctness. Profile before optimizing and measure changes to registered hot paths. Make ordering explicit.
- Change internal interfaces with their callers. Follow semver for external APIs and preserve exact pins.
- Update affected docs. Read the book's coding gotchas before coding, reviewing, or testing. Record race and flaky-test lessons there.

## Procedures

Read only the relevant section of [~/.agents/WORKFLOWS.md](WORKFLOWS.md) for approvals, tests, compatibility, docs and plans, delegation and BB, Git, or reviews. Follow repository delivery rules. My repositories default to direct commits and pushes to `main`. Shared repositories default to a branch and draft PR. The Charter governs tracked delivery.

## Communication

Use plain English. Give brief progress updates during long work. End with the result, evidence, and branch, commit, push, merge, review, and open decisions when applicable.
