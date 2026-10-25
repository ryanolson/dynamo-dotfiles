# Padding patterns

Models pad because length was rewarded in training: judges prefer longer answers ([Length-Controlled AlpacaEval](https://arxiv.org/abs/2404.04475)). Anthropic's guidance for written deliverables is "do not pad with filler sections, redundant summaries, or boilerplate" ([Prompting Claude Opus 5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5)). Cut words, not facts: a terse text that drops a fact the reader needs is also wrong.

| Pattern | Before | After |
|---|---|---|
| Preamble | This PR introduces a new retry mechanism that aims to improve reliability. | Retry failed sends three times. |
| Restating the code | `// Increment the counter` | (delete) |
| Closing summary | In summary, these changes make the system more robust. | (delete) |
| Hedge | It is worth noting that this may potentially cause issues. | This breaks callers that pass `None`. |
| Three benefits | faster, safer, and more maintainable | p99 fell from 12 ms to 8 ms. |
| Invented motive | Refactored for clarity. | (delete, or name the actual bug) |
| Dash aside | The cache — which is bounded — evicts LRU. | The cache is bounded. It evicts the least recently used entry. |
| Test list | Tests: test_a, test_b, test_c all pass. | `cargo test -p x`: 42 passed. |
| Signature restated | `id`: the id (u64). | (delete) |
| History in a comment | // Previously this used a mutex, but we switched to... | (delete; git has it) |

The slop-to-plain word table is in `rules.md`, under "Slop-to-simple substitutions".

## Report rewrite

A bake-off report opened like this, and put its recommendation in section 8 of 8:

> Private report. Source of every number: the scoreboard `bakeoff.md` in bb thread storage of the coordinator thread `thr_…` (`~/.bb-machines/…/thread-storage/thr_…/`, cited as `:line`), and the review files beside it (cited by name). Where the scoreboard prose and a review file disagree, this report uses the review file and says so. Run: 2026-10-22 to 2026-10-24, cells tasks T1 to T11a in dynamo-gdl.

Then a model table, then a "Summary" of five dense bullets of ratios and score vectors. A reader who stops after the first screen learns where the data lives, not what was asked or what to do.

The same facts, in order:

> We want to know whether Kimi K3 can implement concurrency-heavy Rust features as well as Opus 5.5, so that agent swarms can route that work to it. Both models got the same ten cells tasks in dynamo-gdl, each in its own worktree on the same base. A blinded Codex reviewer scored every round until it approved.
>
> **TL;DR**
> - Opus won all 10 tasks. It reached a clean review on all 10; K3 reached one on 2.
> - K3 took about 2x as long to a first implementation, and had about 4 major findings per first review against Opus's 1.
> - Keep Opus as the implementer for this work. Do not route it to K3 in the current setup.
> - A clean verdict missed real defects on both sides. Keep a cross-family reviewer, and cross-read when two implementations exist.

Method, per-task tables, confounders, and lessons follow. The source paths and thread IDs go in a closing "Sources" section.
