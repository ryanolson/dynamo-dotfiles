---
name: sgc-thread
description: Start and run a Stargate Command (SGC) thread. Use when Ryan says "this is an SGC thread", or asks to plan, drive, or report on Stargate Command work, whether or not a Linear issue exists.
user-invocable: true
---

# SGC thread

Ryan opens an SGC thread with a topic. A Linear issue may or may not exist.

1. Follow the `linear-work` skill to read the Charter and the newest standing orders. Also read the change log, a Linear document named like the Charter (`list_documents`, query `change log`). Use `linear-work` for all Linear reads and writes.
2. If Ryan gives an issue, work from it and report on it. If not, ask whether to create one.
3. If Ryan names a mode, such as "start planning", do that. If the topic or mode is unclear, ask once.
4. Report progress as a comment on the issue, signed. Ask decisions with `bb sgc ask`: one question, at most 3 options. Never answer one for Ryan.
5. The steward (`thr_mhr46aqv4x`) keeps Linear moving. Do not message it unless asked.
6. End every turn with one `Next:` line: the owner, the exact next action, and the condition that completes it. Do routine, reversible work your brief or the standing orders cover. If another thread owns the next step, `bb thread tell` it the evidence, the action and the completion condition before you go idle. If only Ryan can decide (scope or public contract change, irreversible action, gated spend or run, merge without delegated authority), `bb sgc ask` with the recommendation first and put its id in the `Next:` line. A `Next:` line that names Ryan always comes with a question in the same turn: `bb sgc ask`, or the question dialog when he is talking to you in this thread. Never end on a statement that waits for him (Ryan, 2026-11-22: "Either the BB ask or the sgc ask should be used to get my attention"). When the outcome is complete, name who closes it.
