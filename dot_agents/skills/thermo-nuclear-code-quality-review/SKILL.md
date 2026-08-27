---
name: thermo-nuclear-code-quality-review
description: Run an extremely strict adversarial review for correctness, hot-path performance, abstraction quality, giant files, and spaghetti-condition growth. Use for a thermo-nuclear code quality review, thermonuclear review, deep code quality audit, or especially harsh maintainability review.
---

# Thermo-Nuclear Code Quality Review

Use this skill for an unusually strict review focused on correctness, performance, implementation
quality, maintainability, abstraction quality, and codebase health.

Above all, this skill should push the reviewer to be **ambitious** about code structure. Do not
merely identify local cleanup opportunities. Actively search for "code judo" moves: restructurings
that preserve behavior while making the implementation dramatically simpler, smaller, more direct,
and more elegant.

Adapted from `ai-dynamo/rhino@a63a717` (`.agents/skills/thermo-nuclear-code-quality-review`), which
itself derives from Cursor's `cursor-team-kit` agent at `b8f2564`, merged with the adversarial pass,
performance pass, and independent-verification step from `ishandhanani/dotfiles@7b54a8c`
(`agents/skills/deep-code-review`). See `LICENSE`.

## Workflow

1. Establish the review target:
   - If the user provides a diff or files, review only that scope.
   - Otherwise inspect the current branch against the likely base, usually `main`, using
     `git diff --stat`, `git diff`, and changed-file contents.
   - Check file sizes for changed files so crossings around 1000 lines are visible.
2. Apply the rubric below only to what the diff and surrounding contents support. Trace cross-file
   impact when module boundaries are touched.
3. Use subagents when distinct review surfaces benefit from parallel inspection. Keep each pass
   scoped and independently verify its findings. An adversarial stage must not run the model that
   produced the claim.
4. Load and use `phone-a-friend` for at least one independent, read-only verification pass. Give it
   the raw artifact and the constraints **without your conclusions**, then verify material claims
   locally before including them. If it is unavailable, say so and continue.
5. Report findings first, ordered by severity, with `file_path:line_number` references.
6. Skip cosmetic nits when structural issues exist. Prefer a small number of high-conviction
   findings over a long list of minor comments.

## Core Prompt

Start from this baseline:

> Perform a deep, adversarial code quality audit of the current branch's changes.
> Rethink how to structure / implement the changes to meaningfully improve correctness, performance,
> and code quality without changing intended behavior.
> Work to improve abstractions, modularity, reduce spaghetti code, improve succinctness and legibility.
> Be ambitious: if there is a clear path to improving the implementation that involves restructuring
> some of the codebase, go for it.
> Be extremely thorough and rigorous. Measure twice, cut once.

## Adversarial Pass

Challenge the change as a whole before reviewing individual lines:

- Ask whether the implementation solves the actual problem in the owning layer or merely patches a
  symptom.
- Find ways tests can pass while production behavior still fails: defaults, error paths, partial
  rollout, rollback, recovery, and cross-component interactions.
- Identify hidden assumptions about ordering, ownership, lifecycle, compatibility, input shape,
  scale, and failure isolation.
- Trace changed contracts through callers and consumers rather than trusting the local diff.
- Compare against a materially simpler or safer direction when one is concrete.
- Report only adversarial concerns with a plausible causal path. Do not manufacture hypothetical
  failures.

Where the repository's practice is test-first validation, prefer a failing test over an argument: a
claim confirmed by reading code is an opinion, a claim confirmed by a failing test is a fact.

## Performance Pass

Trace each changed hot path end to end and compare the work before and after the change:

- Check allocations, cloning, boxing, dynamic dispatch, collection materialization, formatting,
  serialization, and copies.
- Check extra scans, branches, lookups, call depth, synchronization, lock scope, atomics, syscalls,
  IPC, and retries.
- Check whether the change loses batching, locality, cache friendliness, short-circuiting,
  boundedness, or backpressure.
- Check CPU, memory, latency, throughput, file-descriptor, task, queue, and connection growth at
  expected production scale.
- Distinguish hot-path cost from irrelevant cold-path micro-optimization.
- Use existing benchmarks or profiles when practical. Otherwise require a precise causal argument
  and the smallest measurement that would prove or disprove the risk.

## Non-Negotiable Additional Standards

Apply the baseline prompt above, plus these explicit review rules:

0. **Be ambitious about structural simplification.**
   - Do not stop at "this could be a bit cleaner."
   - Look for opportunities to reframe the change so that whole branches, helpers, modes,
     conditionals, or layers disappear entirely.
   - Prefer the solution that makes the code feel inevitable in hindsight.
   - Assume there is often a "code judo" move available: a re-organization that uses the existing
     architecture more effectively and makes the change dramatically simpler and more elegant.
   - If you see a path to delete complexity rather than rearrange it, push hard for that path.

1. **Do not let a PR push a file from under 1k lines to over 1k lines without a very strong reason.**
   - Treat this as a strong code-quality smell by default.
   - Prefer extracting helpers, subcomponents, modules, or local abstractions instead of letting a
     file sprawl past 1000 lines.
   - If the diff crosses that threshold, explicitly ask whether the code should be decomposed first.
   - Only waive this if there is a compelling structural reason and the resulting file is still
     clearly organized.

2. **Do not allow random spaghetti growth in existing code.**
   - Be highly suspicious of new ad-hoc conditionals, scattered special cases, or one-off branches
     inserted into unrelated flows.
   - If a change adds "weird if statements in random places", treat that as a design problem, not a
     stylistic nit.
   - Prefer pushing the logic into a dedicated abstraction, helper, state machine, policy object, or
     separate module instead of tangling an existing path.
   - Call out changes that make the surrounding code harder to reason about, even if they
     technically work.

3. **Bias toward cleaning the design, not just accepting working code.**
   - If behavior can stay the same while the structure becomes meaningfully cleaner, push for the
     cleaner version.
   - Do not rubber-stamp "it works" implementations that leave the codebase messier.
   - Strongly prefer simplifications that remove moving pieces altogether over refactors that merely
     spread the same complexity around.

4. **Prefer direct, boring, maintainable code over hacky or magical code.**
   - Treat brittle, ad-hoc, or "magic" behavior as a code-quality problem.
   - Be skeptical of generic mechanisms that hide simple data-shape assumptions.
   - Flag thin abstractions, identity wrappers, or pass-through helpers that add indirection without
     buying clarity.

5. **Push hard on type and boundary cleanliness when they affect maintainability.**
   - Question unnecessary optionality, `unknown`, `any`, or cast-heavy code when a clearer type
     boundary could exist.
   - Prefer explicit typed models or shared contracts over loosely-shaped ad-hoc objects.
   - If a branch relies on silent fallback to paper over an unclear invariant, ask whether the
     boundary should be made explicit instead.

6. **Keep logic in the canonical layer and reuse existing helpers.**
   - Call out feature logic leaking into shared paths or implementation details leaking through APIs.
   - Prefer existing canonical utilities/helpers over bespoke one-offs.
   - Push code toward the right package, service, or module instead of normalizing architectural
     drift.

7. **Treat unnecessary sequential orchestration and non-atomic updates as design smells when the
   cleaner structure is obvious.**
   - If independent work is serialized for no good reason, ask whether the flow should run in
     parallel instead.
   - If related updates can leave state half-applied, push for a more atomic structure.
   - Do not over-index on micro-optimizations, but do flag avoidable orchestration complexity that
     makes the implementation more brittle.

8. **Documentation must stay consistent with the code the diff touches.**
   - Flag README text, module docs, doc comments, examples, file trees, and build or run commands
     that the change has invalidated.
   - Flag orphaned references to symbols the diff removed or renamed.
   - Treat a comment that restates the code as noise and a comment that records the failure mode a
     design avoids as load-bearing.

## Primary Review Questions

For every meaningful change, ask:

- Is there a "code judo" move that would make this dramatically simpler?
- Can this change be reframed so fewer concepts, branches, or helper layers are needed?
- Does this improve or worsen the local architecture?
- Did the diff add branching complexity where a better abstraction should exist?
- Did a previously cohesive module become more coupled, more stateful, or harder to scan?
- Is this logic living in the right file and layer?
- Did this change enlarge a file or component past a healthy size boundary?
- Are there repeated conditionals that signal a missing model or missing helper?
- Is the implementation direct and legible, or does it rely on special cases and incidental control
  flow?
- Is this abstraction actually earning its keep, or is it just a wrapper?
- Did the diff introduce casts, optionality, or ad-hoc object shapes that obscure the real invariant?
- Is this logic living in the canonical layer, or did the diff leak details across a boundary?
- Is this orchestration more sequential or less atomic than it needs to be?
- Can the change pass its tests while failing under partial rollout, recovery, concurrency, or
  production-scale inputs?
- What new work occurs per request, token, item, iteration, or connection on the hot path?
- Did the change add allocations, copies, dynamic dispatch, synchronization, serialization, or
  resource growth without evidence that the cost is acceptable?
- Did the change leave any documentation, comment, or example describing behavior that no longer
  exists?

## What to Flag Aggressively

Escalate findings when you see:

- Locally correct changes that break callers, consumers, rollout, recovery, or failure isolation.
- New hot-path allocations, copies, materialization, dynamic dispatch, synchronization, IPC, or
  repeated scans without a demonstrated need.
- Performance claims supported only by intuition when an existing benchmark or targeted measurement
  is available.
- A complicated implementation where a cleaner reframing could delete whole categories of complexity.
- Refactors that move code around but fail to reduce the number of concepts a reader must hold in
  their head.
- A file crossing 1000 lines due to the PR, especially if the new code could be split out.
- New conditionals bolted onto unrelated code paths.
- One-off booleans, nullable modes, or flags that complicate existing control flow.
- Feature-specific logic leaking into general-purpose modules.
- Generic "magic" handling that hides simple structure and makes the code harder to reason about.
- Thin wrappers or identity abstractions that add indirection without simplifying anything.
- Unnecessary casts, `any`, `unknown`, or optional params that muddy the real contract.
- Copy-pasted logic instead of extracted helpers.
- Narrow edge-case handling implemented in the middle of an already busy function.
- Refactors that technically pass tests but make the code less modular or less readable.
- "Temporary" branching that is likely to become permanent debt.
- Bespoke helpers where the codebase already has a canonical utility for the job.
- Logic added in the wrong layer/package when it should live somewhere more central.
- Sequential async flow where obviously independent work could stay simpler and clearer with
  parallel execution.
- Partial-update logic that leaves state less atomic than necessary.
- Documentation or comments the diff has silently falsified.

## Preferred Remedies

When you identify a problem, prefer suggestions like:

- Delete a whole layer of indirection rather than polishing it.
- Reframe the state model so conditionals disappear instead of getting centralized.
- Change the ownership boundary so the feature becomes a natural extension of an existing
  abstraction.
- Turn special-case logic into a simpler default flow with fewer exceptions.
- Extract a helper or pure function.
- Split a large file into smaller focused modules.
- Move feature-specific logic behind a dedicated abstraction.
- Replace condition chains with a typed model or explicit dispatcher.
- Separate orchestration from business logic.
- Collapse duplicate branches into a single clearer flow.
- Delete wrappers that do not meaningfully clarify the API.
- Reuse the existing canonical helper instead of introducing a near-duplicate.
- Make type boundaries more explicit so the control flow gets simpler.
- Move the logic to the package/module/layer that already owns the concept.
- Parallelize independent work when that also simplifies the orchestration.
- Restructure related updates into a more atomic flow when partial state would be harder to reason
  about.
- Keep hot-path data borrowed, statically dispatched, batched, and allocation-free where the
  existing design allows it.
- Preserve bounded queues and backpressure instead of hiding overload behind buffering or retries.
- Use the repository's existing benchmark or profiler to measure the smallest representative path.

Do not be satisfied with "maybe rename this" feedback when the real issue is structural.
Do not be satisfied with a merely cleaner version of the same messy idea if there is a plausible
path to a much simpler idea.

## Review Tone

Be direct, serious, and demanding about quality.
Do not be rude, but do not soften major maintainability issues into mild suggestions.
If the code is making the codebase messier, say so clearly.
If the implementation missed an opportunity for a dramatic simplification, say that clearly too.

Good phrases:

- `this pushes the file past 1k lines. can we decompose this first?`
- `this adds another special-case branch into an already busy flow. can we move this behind its own abstraction?`
- `this works, but it makes the surrounding code more spaghetti. let's keep the behavior and restructure the implementation.`
- `this feels like feature logic leaking into a shared path. can we isolate it?`
- `this abstraction seems unnecessary. can we just keep the direct flow?`
- `why does this need a cast / optional here? can we make the boundary more explicit instead?`
- `this looks like a bespoke helper for something we already have elsewhere. can we reuse the canonical one?`
- `i think there's a code-judo move here that makes this much simpler. can we reframe this so these branches disappear?`
- `this refactor moves complexity around, but doesn't really delete it. is there a way to make the model itself simpler?`
- `this passes its tests, but trace the recovery path — the contract changed for every caller.`

## Output Expectations

Prioritize findings in this order:

1. Adversarial correctness, contract, rollout, and recovery failures
2. Hot-path performance and resource regressions
3. Structural code-quality regressions
4. Missed opportunities for dramatic simplification / code-judo restructuring
5. Spaghetti / branching complexity increases
6. Boundary / abstraction / type-contract problems that make the code harder to reason about
7. File-size, modularity, decomposition, and maintainability concerns
8. Documentation and comment drift introduced by the change

Do not flood the review with low-value nits if there are larger structural issues.
Prefer a smaller number of high-conviction comments over a long list of cosmetic notes.

## Approval Bar

Do not approve merely because behavior seems correct.
The bar for approval is:

- no material cross-boundary failure mode hidden by locally passing tests
- no unjustified hot-path allocation, copying, synchronization, serialization, or resource growth
- no performance-sensitive change left unmeasured when a targeted benchmark is practical
- no clear structural regression
- no obvious missed opportunity to make the implementation dramatically simpler when such a path is
  visible
- no unjustified file-size explosion
- no obvious spaghetti-growth from special-case branching
- no obviously hacky or magical abstraction that makes the code harder to reason about
- no unnecessary wrapper/cast/optionality churn obscuring the real design
- no clear architecture-boundary leak or avoidable canonical-helper duplication
- no missed opportunity for an obvious decomposition that would materially improve maintainability
- no documentation the change has left describing behavior that no longer exists

Treat these as presumptive blockers unless the author can justify them clearly:

- the change is locally correct but breaks a caller, consumer, rollout, or recovery path
- the change adds hot-path cost with no measurement and no causal argument
- the PR preserves a lot of incidental complexity when there is a plausible code-judo move that
  would delete it
- the PR pushes a file from below 1000 lines to above 1000 lines
- the PR adds ad-hoc branching that makes an existing flow more tangled
- the PR solves a local problem by scattering feature checks across shared code
- the PR adds an unnecessary abstraction, wrapper, or cast-heavy contract that makes the design more
  indirect
- the PR duplicates an existing helper or puts logic in the wrong layer when there is a clear
  canonical home

If those conditions are not met, leave explicit, actionable feedback and push for a cleaner
decomposition.
