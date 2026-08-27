---
name: rust-code-review
description: Review Rust changes for correctness, performance, concurrency, and maintainability.
---

# Rust Code Review

Adapted from `ishandhanani/dotfiles@7b54a8c` (`agents/skills/rust-code-review`).

Review recently modified Rust code with strict systems and performance standards. Apply the rules below rigorously.

## Core Review Philosophy

- **Simplicity over cleverness**: Flag over-engineered abstractions. Prefer straightforward, readable code.
- **Concise, optimized code**: Minimize ceremony and docstrings. Question verbose documentation.
- **Systems-level thinking**: Consider memory allocation, async runtime behavior, lock contention, and latency.
- **Rust idioms**: Favor `Result`-based error handling with `anyhow`/`thiserror` where the project uses them. Watch for unnecessary `clone()`, `unwrap()` in non-test code, and needless `Arc`/`Mutex`.
- **Correctness in concurrent code**: Scrutinize `tokio`, channels, cancellation, and shared state carefully.
- **Clear, direct naming**: Flag vague names; prefer short, precise identifiers.
- **Minimal diff surface**: Call out unrelated changes mixed into a PR.
- **Logging and observability**: Ensure `tracing` spans and events are meaningful, not noisy.

Use a direct, concise, technically grounded tone. Avoid filler praise. Keep most review comments to one or two lines.

## How to Review

Unless explicitly told otherwise, review only recently written or modified code. Use `git diff`, `git log`, or ask for the specific files or PR if unclear.

1. Identify the review target with `git status`, `git diff --stat`, and `git diff`.
2. Make multiple passes over the code using every rubric below. Keep finding issues and style comments until no more remain.
3. Write the review:
   - Prefer concrete `file:line` findings over general advice.
   - Group issues by severity and include style comments.

## Review Rules

Apply these rules on each pass over the changed code.

1. **No `unwrap()` / `expect()` in production code.** If unavoidable, explain why it cannot fail.
2. **Use the `tracing` crate, never `log`.** The interfaces differ subtly. Delete `use tracing as log;` because it is confusing.
3. **Use structured tracing fields, not formatted strings.** `tracing::error!(error = %e, component_name, "Unable to register service for discovery")` is better than `error!("Unable to register service for discovery: {}", e)`. Use `%` for `Display` and `?` for `Debug`.
4. **Use the right log level.** Reserve `info!` for events end users need to see. Use `debug!` for routine internal events. Use `trace!` or remove logs from hot paths. Logging is relatively expensive because it takes a lock on the output channel.
5. **Do not add `Arc<Mutex<…>>` reflexively.** Avoid synchronization when work is not concurrent. Using both `Arc` and `Box` rarely makes sense because both are pointers; require a comment when both are necessary. Let owners decide synchronization rather than pre-wrapping shared state in a constructor.
6. **Do not add another `Arc` around cheaply cloneable types.** Prefer the type's existing `Clone` implementation when it is cheap and intended for this ownership model.
7. **Drop unnecessary `.clone()`.** Pass a reference, move the value, or make it `Copy` where appropriate. Do not call `.clone()` on `Copy` types.
8. **Prefer `parking_lot::RwLock` over `tokio::sync::RwLock`** for short critical sections when no `.await` is held across the lock.
9. **Use `Drop` for cleanup, not manual unlock paths.** Prefer RAII over ad-hoc cleanup.
10. **Prefer stdlib or Tokio primitives over new dependencies.** Avoid new dependencies when possible.
11. **Do not change error messages or interfaces just for taste.** Rename only when the existing name actively misleads; for example, `serve` implies a long-running server and `Instance` is too generic in a multi-instance system.
12. **Call out scope creep.** A PR should do one thing well. Flag parts unrelated to the rest of the PR.
13. **Focus on async Rust.** Check for locks held across `.await`, blocking work on executor threads, spawned-task shutdown and error handling, cancellation behavior, and channel backpressure.
14. **Avoid unnecessary heap allocation on all paths.**

## Comment Hygiene

- Delete comments that repeat the code or function name.
- Do not put history in comments; use `git`.
- Treat AI-generated comments as a smell. Ask the author to remove verbose or obvious comments and make the remainder more useful.
- Treat AI-generated tests as a smell. Prefer the three most important behavioral tests over long lists that enumerate inputs.
- Keep `///` documentation and `//` internal comments distinct.
- Keep copyright headers to the two required SPDX lines; trim anything beyond that.

## Concurrency and Async Patterns

- Write Tokio sleep as fully qualified `tokio::time::sleep`. Write the stdlib version as `sleep` with `use std::thread::sleep` so the two are distinct.
- Question `Unbounded*` channels because they can OOM the process. Require a justification when using them. Treat bounded channels as defense in depth, not sizing for the happy path.
- Question `tokio::spawn`; keep work inline when spawning adds no value.

## Naming

- Do not make names imply more than they do. For example, `serve` implies a long-running server, and a name should not claim DNS resolution when it does not resolve DNS.
- Prefix boolean variables and functions with `is_`, `needs_`, or `has_` to make their truthy meaning obvious. Prefer `has_admin_permissions(u: &User)` to `admin_permissions(u: &User)`.
- Prefer a parent-level file named for its module over `mod.rs`; for example, use `name.rs` for a `name/` module.
- Do not preserve underscore prefixes on variables that are used: rename `_text` to `text`.

## Tests

- Prioritize behavior coverage over line coverage. Confirm that new logic is exercised, not merely touched.
- Be skeptical of long lists of similar test cases, especially generated ones. Push for the three most important tests.
- Ensure tests are discoverable by the repository's CI configuration. Flag missing required markers, tags, or registration.

## Second Pass Checklist

Before finalizing, make one focused pass over every changed hunk for the review rules, comment hygiene, concurrency and async patterns, naming, and tests. Report all findings.
