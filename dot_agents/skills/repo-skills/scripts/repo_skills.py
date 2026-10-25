"""Compare personal skills with the copies in a repository, and copy them in.

The script never runs git commands that change state: it reports status and
copies files. The agent asks the owner and commits through the repository's
own process.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

MANIFEST = '.skill-sources.json'
PERSONAL_ORGS = {'ryanolson', 'nvidia-dev'}
TEAM_ORGS = {'ai-dynamo'}
SKIP_NAMES = {MANIFEST, '__pycache__', '.DS_Store'}
# References that a teammate's agent cannot follow. A skill with a hit is not copied.
NONPORTABLE = [
    ('ENVIRONMENT.md', 'owner environment guide'),
    ('~/.agents', 'owner skill tree'),
    ('~/.local/state', 'owner state path'),
    ('BB_THREAD_ID', 'bb thread'),
    ('bb thread', 'bb thread'),
    ('phone-a-friend', 'owner-only skill'),
    ('linear-work', 'owner-only skill'),
    ('openv', 'owner secrets tool'),
    ('Model routing', 'owner routing table'),
    ('chezmoi', 'owner dotfiles'),
]


def files_of(root):
    if not root.is_dir():
        return {}
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_NAMES)
        for name in sorted(filenames):
            if name in SKIP_NAMES:
                continue
            path = Path(dirpath) / name
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def tree_hash(root):
    files = files_of(root)
    if not files:
        return None
    h = hashlib.sha256()
    for rel, data in sorted(files.items()):
        h.update(rel.encode() + b'\0' + hashlib.sha256(data).digest())
    return h.hexdigest()


def org_class(repo):
    r = subprocess.run(['git', '-C', str(repo), 'remote', 'get-url', 'origin'], capture_output=True, text=True)
    m = re.search(r'[:/]([^/:]+)/[^/]+?(?:\.git)?/?$', r.stdout.strip())
    org = m.group(1).lower() if m else ''
    if org in PERSONAL_ORGS:
        return org, 'personal'
    if org in TEAM_ORGS:
        return org, 'team'
    return org, 'unknown'


def skills_dir(repo):
    agents, claude = repo / '.agents' / 'skills', repo / '.claude' / 'skills'
    gaps = []
    if agents.is_dir():
        if not claude.exists():
            gaps.append('.claude/skills is missing, so Claude Code does not see these skills')
        return agents, gaps
    if claude.is_dir():
        gaps.append('.agents/skills is missing, so Codex does not see these skills')
        return claude, gaps
    return agents, ['no skills directory yet; .agents/skills will be created']


def portability(root):
    hits = []
    for rel, data in files_of(root).items():
        for n, line in enumerate(data.decode(errors='replace').splitlines(), 1):
            for needle, why in NONPORTABLE:
                if needle in line:
                    hits.append(f'{rel}:{n}: {needle} ({why})')
    return hits


def load_manifest(sdir):
    path = sdir / MANIFEST
    return json.loads(path.read_text()) if path.exists() else {'skills': {}}


def save_manifest(sdir, manifest):
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')


def classify(base, mine, theirs):
    if theirs is None:
        return 'new'
    if base is None:
        return 'same' if mine == theirs else 'first-contact'
    if mine == theirs:
        return 'unchanged' if mine == base else 'converged'
    if theirs == base:
        return 'mine-updated'
    if mine == base:
        return 'theirs-updated'
    return 'both-changed'


def status(repo, source, names):
    org, cls = org_class(repo)
    sdir, gaps = skills_dir(repo)
    out = {'repo': str(repo), 'org': org, 'org_class': cls, 'skills_dir': sdir.relative_to(repo).as_posix(),
           'gaps': gaps, 'skills': [], 'repo_only': []}
    if cls == 'personal':
        out['gaps'] = []
        return out
    manifest = load_manifest(sdir)
    ours = {d.name for d in source.iterdir() if (d / 'SKILL.md').exists()}
    theirs_names = {d.name for d in sdir.iterdir() if (d / 'SKILL.md').exists()} if sdir.is_dir() else set()
    out['repo_only'] = sorted(theirs_names - ours)
    if names is None:
        names = sorted(set(manifest['skills']) | (ours & theirs_names))
    for name in names:
        base = manifest['skills'].get(name, {}).get('hash')
        mine, theirs = tree_hash(source / name), tree_hash(sdir / name)
        out['skills'].append({'name': name, 'state': classify(base, mine, theirs),
                              'portability': portability(source / name)})
    return out


def record(sdir, name, digest):
    manifest = load_manifest(sdir)
    manifest['skills'][name] = {'hash': digest, 'synced': datetime.date.today().isoformat()}
    save_manifest(sdir, manifest)


def copy(repo, source, name, allow_nonportable, overwrite):
    sdir, _ = skills_dir(repo)
    hits = portability(source / name)
    if hits and not allow_nonportable:
        sys.exit(f'{name}: not portable; rewrite these references first:\n  ' + '\n  '.join(hits))
    base = load_manifest(sdir)['skills'].get(name, {}).get('hash')
    theirs = tree_hash(sdir / name)
    if theirs is not None and theirs != base and not overwrite:
        sys.exit(f'{name}: the repository copy differs from the last sync; review it before overwriting (--overwrite)')
    target = sdir / name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source / name, target, ignore=shutil.ignore_patterns(*SKIP_NAMES))
    record(sdir, name, tree_hash(target))
    print(f'copied {name} to {target.relative_to(repo)}')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['status', 'copy', 'mark'])
    p.add_argument('skills', nargs='*', help='skill names; status defaults to synced skills and skills both sides have')
    p.add_argument('--all', action='store_true', help='status: every personal skill')
    p.add_argument('--repo', type=Path, default=Path.cwd())
    p.add_argument('--source', type=Path, default=Path.home() / '.agents' / 'skills')
    p.add_argument('--json', action='store_true')
    p.add_argument('--allow-nonportable', action='store_true')
    p.add_argument('--overwrite', action='store_true')
    a = p.parse_intermixed_args()
    repo, source = a.repo.resolve(), a.source.resolve()
    if a.command == 'status':
        names = a.skills or (sorted(d.name for d in source.iterdir() if (d / 'SKILL.md').exists()) if a.all else None)
        out = status(repo, source, names)
        if a.json:
            print(json.dumps(out, indent=2))
            return
        print(f"{out['repo']}: {out['org']} ({out['org_class']}), skills in {out['skills_dir']}")
        for g in out['gaps']:
            print(f'  gap: {g}')
        if out['org_class'] == 'personal':
            print('  personal repository: it uses the owner skills directly; nothing to sync')
        for s in out['skills']:
            flag = f"  [not portable: {len(s['portability'])} references]" if s['portability'] else ''
            print(f"  {s['state']:15} {s['name']}{flag}")
        if out['repo_only']:
            print(f"  repository-only skills (check for trigger overlap): {', '.join(out['repo_only'])}")
        return
    if not a.skills:
        p.error(f'{a.command} needs at least one skill name')
    if org_class(repo)[1] == 'personal':
        sys.exit('personal repository: it uses the owner skills directly; nothing to sync')
    for name in a.skills:
        if a.command == 'copy':
            copy(repo, source, name, a.allow_nonportable, a.overwrite)
        else:
            sdir, _ = skills_dir(repo)
            if tree_hash(sdir / name) is None:
                sys.exit(f'{name}: no copy in the repository to mark')
            record(sdir, name, tree_hash(sdir / name))
            print(f'marked {name}: the repository copy is now the sync base')


if __name__ == '__main__':
    main()
