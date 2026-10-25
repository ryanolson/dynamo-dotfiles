# Register entry format

The register chapter opens with one paragraph: which workloads were profiled, on which host, and when. Then one section per path. The section heading is the entry name that the code marker uses.

```markdown
## frame-decode

- **Entry point:** `lib/velo/src/transports/tcp/listener.rs:212` `read_frames`
- **Why it is hot:** every inbound message passes through it.
- **Profile:** `perf record --call-graph dwarf -- target/release/examples/ping_pong --msgs 1000000` at `abc1234`, 2026-10-25, on host-a (idle). 31 percent of on-CPU samples, 19 percent in `BytesMut::split_to` and `memcpy` below it.
- **Guard:** `cargo bench -p velo --bench frames -- decode`. Baseline 412 ns per frame, noise band plus or minus 2.5 percent (criterion confidence interval, 10 runs).
- **Sensitive to:** copies of the payload, a lock on the receive path, a new allocation per frame.
```

Rules for an entry:

- The profile line has the exact command, the commit, the date, the host, and the share with its denominator.
- The guard line has a command that a teammate can run, its baseline, and its measured noise band.
- "Sensitive to" names the kinds of change that regressed this path or would. Keep it to one line.
- No plans or ticket identifiers. A planned speedup goes in the project's plan store.
- An entry whose profile is older than the last release is stale. Refresh or remove it at the release pass.
