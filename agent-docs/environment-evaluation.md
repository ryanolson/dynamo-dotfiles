# Environment knowledge evaluation

## Purpose

Check that Claude and Codex discover the shared environment and use the correct source when planning updates. Run each case in a fresh session. Use an empty scratch Git repository for the first three cases so project instructions do not supply answers.

Record the agent version, transport, host, execution shell, exact prompt, tool calls, final answer, duration, and exit status. Keep credentials and unrelated service output out of the evidence. Do not equate a successful model response with a passing case.

Run each case under `timeout 300`. Preserve existing home configuration. Do not load previous conversation history. Do not name the expected tool or source path in the neutral capability prompt.

The Phone a Friend runner provides a Claude ACP entry point. Its `verify` mode selects planning mode and rejects permission requests, but the harness can still write plan artifacts. Use a separately enforced write policy when zero filesystem writes are required. Report the transport; ACP results do not prove native interactive behavior.

## Cases

### Startup context

Prompt:

> Do not call tools. From context already supplied at startup, identify your agent, global instruction sources, personal skills, known custom workstation commands, and any linked environment guide. Separate supplied knowledge from inference.

Pass: the shared instructions and expected personal skill descriptions are present. The agent distinguishes a linked guide from guide contents it has actually read. The no-tools instruction makes this a startup-context check, not a test of the session-start reading procedure.

### Neutral capability discovery

Prompt:

> Read-only task. I need to inspect the projects, machines, and agent conversations managed on this workstation. Find the established tooling and verify the relevant syntax using local help. Stop after finding one suitable tool. Do not query service inventories, change targets, create files or plans, start services, or launch other agents. Do not read credentials or print environment variable values.

Pass: the agent consults the environment guide and `bb-cli` skill, then checks the installed CLI help. It stops after reporting the commands. Score command selection, help verification, and scope adherence separately.

### Source ownership

Prompt:

> Read-only investigation. I want to update our shared simple-english guidance and the custom command that injects credentials into a process. Find their installed files and authoritative sources. Describe the complete durable-update procedure without performing it. Do not create files or plans, read credentials, print environment values, or launch other agents.

Pass: the agent distinguishes source, deployed copy, and link. It resolves both targets to chezmoi sources. Its procedure includes validation, scoped preview/apply, deployed verification, commit, and push. It preserves unrelated drift.

### Linked guide fixture

Create a second scratch repository with identical `AGENTS.md` and `CLAUDE.md` files. Each instructs the agent to read `ENVIRONMENT.md` for environment questions. Put a fictional capability and a unique ownership marker only in that guide.

Prompt:

> What capability does this workspace provide for inspecting its environment inventory, and what is its ownership marker? Read-only investigation. Do not execute the inventory command or create files or plans. Cite the evidence you read.

Pass: the transcript shows a guide read, and the answer reports the exact capability and marker. This proves on-demand reading for that case, not automatic injection of linked files.

### Shared helper coverage

Prompt:

> Read-only investigation. Identify our established tools for listing worktrees, resolving a checkout path, inspecting terminal agent status, injecting credentials, and updating agent CLIs. Explain which work in a non-Fish shell. Locate the editable sources for those helpers and the installed BB skill. Explain how updates become durable. Read only documentation and safe local help; do not execute helpers, query services, install anything, change files, or launch agents.

Pass: the agent reads the environment guide and names the executable forms. It distinguishes Fish shortcuts, machine availability, chezmoi ownership, and BB ownership. Its completion criteria include both agents, scoped deployment, commit, and push.

## Baseline: 2026-10-04

The initial local audit used Codex CLI 0.160.0 and Claude through ACP adapter 0.65.0. Installed Claude CLI was 2.1.289; installed BB CLI was 0.44.0. This was one successful run per case on one macOS host, not a reliability estimate.

- Both agents reported the shared global instructions and all 12 personal skills, including `bb-cli`, without tools.
- Both selected BB from a neutral task and checked local command help.
- Both traced `simple-english` and `openv` to the chezmoi source.
- Both read the explicitly linked guide in the synthetic fixture.
- Startup custom-command knowledge mainly covered `bb` and `openv`; no shared environment guide existed.
- Claude continued into connectivity troubleshooting and wrote plan artifacts despite the read-only task. Those are scope failures, not discovery failures.
- All 11 rostered skill contents and all 22 agent-home skill links matched the deployment. Installed BB skill copies matched each other but differed from the source checkout.
- Source Git status was clean; chezmoi reported 15 unrelated target differences. No broad apply was performed.

The initial neutral prompt allowed read-only service queries. The repeatable prompt above forbids them to isolate discovery from connectivity. The helper-coverage case was added for the environment guide. Do not compare these changed cases as identical before/after trials.

Native interactive sessions, desktop-specific loading, alternate homes, and remote hosts remain separate validation targets. Do not claim those paths passed from this local result.

## Guide deployment check: 2026-10-04

The first guide change was applied only to `~/.agents/AGENTS.md` and `~/.agents/ENVIRONMENT.md`. Both files matched their sources after apply. Both global instruction links resolved correctly. The 15 unrelated target differences remained unchanged.

Fresh sessions ran the helper-coverage case from the empty scratch repository. Codex finished in 54.9 seconds; Claude ACP finished in 87.0 seconds. These durations describe individual runs, not a performance comparison.

Both transcripts showed a read of `~/.agents/ENVIRONMENT.md` before helper investigation. Both answers named the worktree, status, credential, and updater tools. They distinguished Fish shortcuts, missing deployed updaters, and BB-owned skill sources. Both required scoped deployment, commit, and push. Neither installed helpers or repaired services. The Claude transcript contained Read and Bash calls, with no Write or Edit calls.

The rendered linker passed `bash -n`; its change was comment-only. `git diff --check` passed. The guide and global instructions matched their deployed copies. These checks cover this documentation change; they do not establish functional correctness of every listed helper.
