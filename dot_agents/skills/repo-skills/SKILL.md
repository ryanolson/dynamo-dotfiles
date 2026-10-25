---
name: repo-skills
description: Copy the owner's personal skills into a public team repository and find where the two copies drifted. Use when syncing skills to an ai-dynamo repo, or before editing a skill there.
---

# Repo Skills

Teammates do not have the owner's personal skills, so a public team repository carries its own copies. This skill keeps those copies and the personal ones in step, and treats a teammate's edit as input, not as drift to overwrite.

## Which repositories

The script classifies the repository by the owner of its `origin` remote:

- `ryanolson` and `NVIDIA-dev`: personal. These use the personal skills directly. Report that and stop.
- `ai-dynamo` (velo, rhino, dynamo): team. Sync through the repository's own review process.
- Anything else: ask the owner.

## Status

```sh
python3 -I ~/.agents/skills/repo-skills/scripts/repo_skills.py status --repo <path> [skill ...] [--all]
```

It compares three versions of each skill by content: the last sync (from `.skill-sources.json` in the repository's skills directory), the personal copy in `~/.agents/skills`, and the repository copy. Running it is safe and needs no approval.

| State | Meaning | Offer |
|---|---|---|
| `new` | Not in the repository | Copy it, if it belongs in the team set |
| `unchanged`, `same`, `converged` | Both copies agree | Nothing |
| `mine-updated` | Only the personal copy changed | Copy it |
| `theirs-updated` | A teammate changed the repository copy | Show the diff. Adopt their change into the personal skill, or keep both. Never overwrite it silently. |
| `both-changed` | Both changed | Show both diffs and propose a merge |
| `first-contact` | The repository has a copy and no sync record | Diff it. When the personal skill says "Adapted from" this repository, the repository is upstream. Merge, then `mark` it. |

It also lists portability problems: references that a teammate's agent cannot follow, such as the owner's environment guide, bb threads, or owner-only skills. Repository-only skills are listed so that you can check them for a trigger that overlaps a personal skill.

## Resolve

Ask the owner before each change, with the diff and your recommendation first.

- `copy <skill>` replaces the repository copy and records the sync. It refuses a skill with portability problems, and a repository copy that changed since the last sync, unless the owner approves `--allow-nonportable` or `--overwrite`.
- `mark <skill>` records the repository copy as the sync base without copying, after you adopt it.
- Make a personal skill portable before copying it: make the personal parts conditional ("if `~/.agents/ENVIRONMENT.md` exists") or move them into a personal-only skill. Edit the personal source in the dotfiles repository, never the deployed copy.
- Keep the repository's layout. Report a missing `.agents/skills` or `.claude/skills` as a gap; do not restructure a team repository unasked.

Deliver through the repository's process: a branch, signed-off commits when it requires DCO, and a draft PR. Each PR needs the owner's approval; an earlier approval does not carry over. Adopted teammate changes go to the dotfiles repository and are committed and pushed there.

## Completion

Status was run, each drifted skill was resolved or deferred by the owner, the sync records match, and the status block names the PR and the dotfiles commit. The weekly agent instructions review runs `status` on each team repository.
