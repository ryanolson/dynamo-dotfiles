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

A model comparison report opened like this, and put its recommendation in the last of eight sections:

> Private report. Source of every number: the scoreboard in the coordinator thread's storage (path, cited as `:line`), and the review files beside it (cited by name). Where the scoreboard prose and a review file disagree, this report uses the review file and says so. Run: three days, tasks T1 to T11.

Then a model table, then a "Summary" of five dense bullets of ratios and score vectors. A reader who stops after the first screen learns where the data lives, not what was asked or what to do.

The same structure, in order (numbers are placeholders):

> We want to know whether model X can implement our concurrency-heavy Rust features as well as model Y, so that agent swarms can route that work to it. Both models got the same N tasks, each in its own worktree on the same base. A blinded reviewer from a third model family scored every round until it approved.
>
> **TL;DR**
> - Y won N of N tasks, and reached a clean review on all of them; X reached one on K.
> - X took about Rx as long to a first implementation, and had about M major findings per first review against Y's 1.
> - Keep Y as the implementer for this work. Do not route it to X in the current setup.
> - A clean verdict missed real defects on both sides. Keep a cross-family reviewer, and cross-read when two implementations exist.

Method, per-task tables, confounders, and lessons follow. Source paths and thread IDs go in a closing "Sources" section.
