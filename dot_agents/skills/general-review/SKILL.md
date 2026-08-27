---
name: general-review
description: Review a design, document, or code change for material correctness, scope, and performance issues.
---

# General Review

Adapted from `ishandhanani/dotfiles@7b54a8c` (`agents/skills/general-review`).

Review the target artifact for material concerns, likely regressions, ambiguous behavior, unnecessary complexity, and performance overhead. Be especially skeptical of refactors and changes that may add hidden cost in hot paths.

Use subagents by default for reviews: each active review bucket should get its own focused pass when subagents are available. Skip subagents only when the user asks not to use them, when subagents are unavailable, or when the target is clearly very small.

## Scope

Treat the review target as the artifact that is either:
- currently opened or referred to in plan mode, or
- contained in the user message immediately before invoking this skill.

Do not do a deep conversation-history dig to reconstruct intent. Do not broaden scope unless the target itself forces it for validation. If the target is ambiguous, prefer the most recent explicit artifact.

Do not treat the target as a git diff, branch diff, or diff review by default. Use diff-review mode only when the user explicitly asks for a patch/diff/PR/branch review, provides an actual diff or patch, or the artifact is obviously a diff. Otherwise, review the conversation, plan, code sketch, document, or scoped code surface as-is. Do not collect git metadata or compare against the working tree unless diff review is explicitly or obviously the task.

Evaluate the target primarily as a delta against the base plan or patch itself.
- Focus on concerns introduced by the proposed change.
- Do not turn the review into a hunt for pre-existing overheads or problems in the surrounding system.
- Only surface pre-existing concerns when they are very severe, or when the proposed change would interact with them poorly, amplify them, or make them harder to fix.
- When mentioning an inherited issue, make clear that it is pre-existing and explain why it matters for this delta.

## Applicable Targets

This skill applies to:
- general conversation where the user wants a concern pass
- informal plans or design sketches
- concrete implementation plans
- documents and proposals
- quick patch snippets or diffs when explicitly requested, supplied, or obvious
- scoped reviews of a subsystem or invariant surface, such as CRTC, JetStream recovery, or local indexer recovery

## What To Review

The target is usually one of:
- a conversation or informal proposal
- a plan
- a document
- a patch or diff when explicitly requested, supplied, or obvious
- a code sketch
- a refactor proposal
- a scoped code surface or subsystem invariant

Review for material issues in these categories:

- Correctness and semantic regression
- Atomicity, transactionality, and partial-commit risk
- In-process concurrency risk
- Distributed consistency, recovery, and invariant guarantees
- Ambiguity, unstated assumptions, and underspecified behavior
- Overhead, especially in hot paths
- Refactor hazards and abstraction cost
- API or contract drift
- Testability and observability gaps

## Priority

Bias toward finding issues that are:
- likely to cause incorrect behavior
- likely to cause logical regression
- likely to add cost on hot paths
- likely to make future changes harder without real benefit
- likely to hide behavior behind abstraction in ways that reduce clarity or performance

Ignore very minor issues unless the user explicitly wants a deep or exhaustive pass.

## High-Level Adversarial Pass

Challenge the target as a whole before dispatching detailed buckets:
- question whether the problem is framed correctly and the proposed direction addresses the actual outcome
- identify hidden assumptions, missing dependencies, and unstated rollout or operational constraints
- look for ways the target can satisfy its local checks while still failing end to end
- examine interactions and second-order effects that no single bucket captures
- compare against a materially simpler or safer direction when one is concrete
- challenge whether the proposed validation can produce false confidence

For non-trivial targets, assign a separate subagent to this adversarial pass. Give it the full target and ask it to attack the premise, integration boundaries, and validation strategy rather than repeat the detailed bucket checklists. Evaluate small targets inline.

## Review Buckets

Select buckets by their primary failure mode:

- incorrect behavior or contract -> Bucket A
- in-process timing, ownership, or lifecycle -> Bucket B
- cross-process ordering, consistency, or recovery -> Bucket C
- maintainability, layering, or architectural fit -> Bucket D
- CPU, memory, latency, or throughput -> Bucket E

Do not flag checklist items mechanically. Report an item only when there is a
plausible causal path to meaningful correctness risk, regression, overhead, or
confusion.

Before dispatching each bucket, make a quick relevance decision from the target
artifact and known context. Do not assign a bucket merely because it exists. If
a bucket is not relevant, mark it inactive and stop exploring it. If a subagent
determines its assigned bucket is not relevant, it must return that conclusion
briefly and stop early rather than speculating, widening scope, or searching for
unrelated concerns.

For every active bucket, the main agent must provide concise dispatch guidance:
- classify the bucket's apparent relevance
- name one to three target-specific risk points worth checking, when visible
- point to the files, symbols, plan steps, or diff regions that motivate them
- explicitly say when a bucket appears marginal and invite an early return

Treat these as hypotheses, not expected findings. Do not lead the subagent toward
agreement or prevent it from finding a different issue inside its assigned bucket.

Assign one subagent to each active canonical bucket by default. Give that subagent
only the target artifact, necessary code or file context, a concise conversation
summary if needed, and the complete checklist for the single canonical bucket it
owns. Explicitly instruct it to stay within that bucket, make an early relevance
check, and stop once the bucket is exhausted. Skip this only when the user opts
out, subagents are unavailable, or the target is clearly very small.

### Bucket A - Semantics, Contracts, and Tests

- logical regression or changed behavior at boundaries
- API, wire-format, schema, or contract drift
- unstated invariants, ambiguous ownership, or unclear rollout/migration assumptions
- implicit behavior changes that the plan or patch does not name
- fallback, error-handling, or guarantee changes that silently weaken behavior
- missing edge cases, mismatched tests, weak assertions, or tests that miss the risky path
- breaking changes without a migration path
- misleading documentation, examples, or configuration guidance

### Bucket B - Concurrency, Atomicity, and Lifecycle

- half-committed state from multi-step or multi-structure updates
- stale plan/publish windows without validation or retry
- data races, lost updates, ABA-style reuse, or inconsistent snapshots
- deadlocks, lock-order inversions, recursive lock hazards, or widened lock scope
- blocking operations in async contexts or locks held across `.await`
- tight loops, busy waits, or retries without yield, sleep, or backoff
- missing cancellation or shutdown paths
- resource lifecycle leaks involving tasks, handles, files, sockets, listeners, or subscriptions
- in-process queue boundedness, backpressure, and producer/consumer shutdown behavior

### Bucket C - Distributed Consistency and Recovery

- unclear atomicity boundaries across process, service, stream, or storage layers
- weak idempotency, replay, deduplication, or ordering guarantees
- recovery gaps after partial failure, restart, reconnect, compaction, or snapshot restore
- stale cursor, generation, lease, epoch, sequence, or version handling that can admit old state
- divergent source-of-truth assumptions between local state, durable logs, caches, and subscribers
- missing backpressure, boundedness, or failure propagation in queues, streams, and subscribers
- distributed invariant violations during rollout, failover, retry, or recovery

### Bucket D - Architecture, Patterns, and Dependencies

- duplicated logic or deviation from established repository patterns without a concrete reason
- stale or deprecated APIs/crates
- unnecessary hand-written replacements for standard facilities or maintained dependencies
- failure to follow existing workspace dependency and layering patterns
- naming or module structure that conflicts with nearby conventions
- dead code, unused imports, or hardcoded values that should be configuration
- additional indirection that obscures data flow, ownership, or invariants
- one-hop methods or trivial wrappers without validation, contract clarity, reuse value, or a meaningful abstraction boundary
- premature generalization that introduces complexity before it is needed
- needlessly complex direction when a simpler approach is likely available

### Bucket E - Performance and Resource Efficiency

- new dynamic dispatch where static dispatch was previously sufficient
- new boxing, heap allocation, trait objects, or avoidable `Arc`/`Mutex` churn
- unnecessary cloning, copying, collection materialization, formatting, or string building
- unnecessary intermediate `Vec`, `HashMap`, or similar collection construction
- extra scans, lookups, retries, branches, call depth, or synchronization in hot paths
- loss of locality, cache friendliness, batching, short-circuiting, or early returns
- avoidable CPU, memory, latency, throughput, file-descriptor, or network overhead
- resource growth that is bounded but operationally excessive under expected load

## Subagent Workflow

When subagents are available, dispatch one subagent per active bucket by default, using the strongest available subagent model in that environment where possible. Pass each subagent only the necessary context: the target artifact, the code or files to inspect, a concise conversation summary if needed, exactly one bucket checklist, and the main agent's concise relevance/risk guidance. Ask for concrete candidate issues with file:line references or precise plan references when available. Tell each subagent not to ponder unrelated possibilities: if the target has no meaningful surface for its bucket, it should say so in one line and return immediately.

Evaluate the active buckets and adversarial pass yourself inline only when the user asks not to use subagents, subagents are unavailable, or the target is clearly very small. If the target is small but still subtle or high-risk, keep the same target artifact for each active bucket rather than sharding by file or subsystem.

While subagents run, the main agent may do only targeted complementary work:
- inspect the highest-risk invariant or user-called-out area
- gather missing local context needed to judge likely findings
- check obvious architecture, API, or ownership fit questions

After subagents report back, the main agent must consolidate and independently evaluate every candidate finding:
- verify whether it is real or a false positive
- read nearby code or plan text as needed
- drop speculative, stylistic, waived, or clearly intentional tradeoffs
- rank remaining concerns by severity and patch direction

The main agent owns the final judgment. Subagent output is input to review, not final output.

## Filtering

Filtering is done by the main agent after bucket reviews return. Subagents should not make final inclusion decisions or shape the final report; they should return candidate findings for their assigned bucket.

The main agent should be aggressive about filtering false positives, but must report every material concern that survives verification.

Do not report:
- cosmetic nits
- style-only preferences
- speculative concerns without evidence
- tradeoffs that are explicitly intended and clearly acceptable
- tiny overhead in code that is obviously cold
- generic "consider adding tests" comments unless the missing test is tied to a concrete risk

Surface every concern that is plausible, consequential, and worth the user's attention.
