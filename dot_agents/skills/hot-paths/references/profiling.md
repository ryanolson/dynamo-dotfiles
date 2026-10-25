# Profiling for the register

Read this before the first measurement.

## Build

- Profile an optimized build with symbols. For Rust, use the release profile with line tables: `CARGO_PROFILE_RELEASE_DEBUG=line-tables-only cargo build --release`. Do not profile a debug build.
- Keep the build fixed across before and after: the same profile, features, toolchain, and target directory.

## Environment

- Fix the command, the input size, and the environment variables. Write them into the entry.
- Record the host and its load (`uptime`, and the other build or test processes running). On a shared host, other agents' builds add noise. Measure the noise band under the same conditions, or move to a quiet host.
- Pin CPU-bound runs to cores with `taskset` when the host has spare cores, and record that you did.

## Sampling profile

- `perf record -F 999 --call-graph dwarf -o target/perf/<name>.data -- <command>`. Use `--call-graph lbr` on hosts that support it for lower overhead.
- `perf report -i target/perf/<name>.data --no-children --sort symbol` for the hot leaves. Add `--children` to see the inclusive cost of the project's own frames.
- For each hot leaf, find the project frame that calls it. The register names the project code, not the library primitive.
- For async Rust, a sampling profile shows executor frames. Attribute through the future's poll function, or add a span timer around the step.

## Second view

- A benchmark that isolates the path (`cargo bench` with criterion, or `hyperfine` for a whole command), or a span timer around the step in the full run.
- The second view must agree with the profile share, after converting thread time to wall time for parallel code. When they disagree, find out why before you add the entry.

## Noise band

- criterion: use its confidence interval over at least 10 samples, and compare against a saved baseline (`--save-baseline`, `--baseline`).
- `hyperfine --warmup 3 --runs 20`: use the spread it reports.
- Record the band in the entry. A later change counts as a regression only outside the band.

## GPU

`nsys profile` shows the GPU timeline, transfers, and host-side launch gaps. Use it to see whether a path is host-bound. Kernel optimization is out of scope for this skill.
