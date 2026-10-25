---
name: hot-paths
description: Keep a profile-backed register of performance-sensitive code paths, guard changes to them, and find speedups from it. Use when a change touches a hot path, or for perf work. Not for GPU kernels.
---

# Hot Paths

Performance is a correctness property on the code paths that carry the load. This skill makes those paths visible: a register in the documentation, a marker in the code, and a benchmark that guards each one. Every entry comes from a profile. A path that someone believes is hot, without a profile, is not an entry.

## The register

- With a book: the chapter `docs/src/operations/hot-paths.md`. Without one: `docs/hot-paths.md`.
- One entry per path, in the format of `references/register.md`: the entry point, the profile that showed it (command, commit, date, host, share of time), the benchmark that guards it, and its noise band.
- In the code, one comment at the entry point: `// hot path: <entry-name>` (or `#` in Python), where the name is the entry's heading. The marker lets an agent that edits the code find the entry with a search.

## Modes

**Build or refresh the register.** Profile the workloads the project cares about, following `references/profiling.md`. Add an entry for each path above about 5 percent of the run, or below that when the call rate makes it matter. Remove an entry that a new profile no longer shows as hot. Refresh the whole register at each release, with the documentation pass.

**Guard a change.** When a change touches a marked function or a file an entry lists, run the entry's benchmark before and after the change on the same host and build. A difference inside the noise band is no change. A slowdown outside it needs the numbers and a reason in the commit or PR, and the owner's decision. A change that makes a path hot, or no longer hot, updates the register.

**Improve.** Start from the register, ranked by share. Profile again to confirm the path is still hot, then name the primitive that costs the time and the code that calls it. Measure before and after with the same recipe. Report the benchmark, the profile share, and the end-to-end number. Keep the change only if the end-to-end number moves outside the noise band.

## Rules

- Two views must agree: a sampling profile and a benchmark or span timer. Drop a claim that only one view supports.
- One denominator per table, named. Report the unattributed remainder as a row.
- Measure the full-size workload for steady-state costs. Use a short run only to check where the time goes.
- Record the host and the machine load. A shared build host adds noise; measure the noise band there, or use a quiet host.
- Write benchmarks the repository can keep (`cargo bench`, criterion, or a `hyperfine` command). Put one-off measurements under `target/`, not in the commit.
- Do not install a profiler unasked. Use what the host has: `perf`, `hyperfine`, and `nsys` for GPU timelines.

GPU kernel work is out of scope. In a repository with its own profiling skills (rhino's `optimize-cpu-work` and `analyze-profile`), use those for their workloads and keep this register as the index.

## Completion

The register entries have profile evidence and a guarding benchmark, every marker has an entry and every entry has a marker, and the report gives before and after numbers with the noise band. The rules above adapt `ai-dynamo/rhino`'s `optimize-cpu-work` methodology.
