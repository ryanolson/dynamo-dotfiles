"""The cluster profiles file of the k8s-clusters skill: load it and validate it.

The schema is references/profiles.md. Shared by k8s-check and k8s-guard, so both
refuse the same bad file the same way. Standard library only.
"""
import os
from pathlib import Path
import pwd
import tomllib

SCHEMA_VERSION = 1
DEFAULT_PROFILES = '~/.config/k8s-clusters/profiles.toml'
ALLOWED = ('first-choice', 'qa-idle', 'never')
ARCHES = ('amd64', 'arm64')
EFFECTS = ('NoSchedule', 'PreferNoSchedule', 'NoExecute')

BOT_KEYS = {'user': str, 'kubeconfig': str, 'developer': str, 'namespace_prefix': str}
# Keys every profile needs, then the ones every profile but a "never" one needs.
BASE_KEYS = {'context': str, 'allowed': str, 'source': list}
USE_KEYS = {'bot_can_write': bool, 'arch': str, 'gpu_product': str, 'gpu_type': str, 'gpus_per_node': int,
            'storage_class': str, 'model_cache': str, 'pull_secret': str, 'reaper': bool,
            'operator_namespace': str}
OPTIONAL_KEYS = {'gpu_taint': dict, 'notes': str}


class ProfileError(Exception):
    """The profiles file is missing (code profiles_missing) or invalid (code profiles_invalid)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


OWNER_DIRS = ('.kube', '.tsh')
TBOT_DIR = '.local/share/tbot'


def account_homes():
    """$HOME and the account's home from the password database: HOME alone can be changed."""
    homes = {os.path.expanduser('~')}
    try:
        homes.add(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        pass
    return homes


def bot_kubeconfig_problem(path):
    """Why path cannot be the bot's kubeconfig, or None when it can.

    An allowlist: the resolved file must lie in tbot's own directory, where the tbot
    config writes it. Path checks by denylist kept leaking (a symlink, a changed HOME,
    a symlink before "..").
    The denylist stays as a second check: no .kube or .tsh directory in the path as
    written or as resolved, since those hold the owner's own login.
    os.path.realpath resolves each symlink before the ".." after it, as the kernel does.
    """
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        return f'bot.kubeconfig {path} must be an absolute path or start with ~'
    real = Path(os.path.realpath(expanded))
    if any(part in OWNER_DIRS for p in (Path(expanded), real) for part in p.parts):
        return f'bot.kubeconfig {path} is in a .kube or .tsh directory, the owner\'s login'
    homes = account_homes()
    for home in homes:
        for d in OWNER_DIRS:
            owner = Path(os.path.realpath(os.path.join(home, d)))
            if real == owner or owner in real.parents:
                return f'bot.kubeconfig {path} resolves into {owner}, the owner\'s login'
    if not any(Path(os.path.realpath(os.path.join(home, TBOT_DIR))) in real.parents for home in homes):
        return f'bot.kubeconfig {path} resolves to {real}, which is not under ~/{TBOT_DIR}'
    return None


def profiles_path():
    return Path(os.path.expanduser(os.environ.get('K8S_CLUSTERS_PROFILES') or DEFAULT_PROFILES))


def check_type(where, key, value, kind, errors):
    # bool is a subclass of int; a profile that says gpus_per_node = true is wrong.
    if kind is int and isinstance(value, bool) or not isinstance(value, kind):
        errors.append(f'{where}.{key} must be a {kind.__name__}')
        return False
    if kind is str and value == '' and key not in ('model_cache', 'pull_secret'):
        errors.append(f'{where}.{key} must not be empty')
        return False
    return True


def validate_cluster(name, prof, errors):
    where = f'clusters.{name}'
    if not isinstance(prof, dict):
        errors.append(f'{where} must be a table')
        return
    never = prof.get('allowed') == 'never'
    known = {**BASE_KEYS, **USE_KEYS, **OPTIONAL_KEYS}
    for key in prof:
        if key not in known:
            errors.append(f'{where}.{key} is not a known key')
    required = BASE_KEYS if never else {**BASE_KEYS, **USE_KEYS}
    for key, kind in required.items():
        if key not in prof:
            errors.append(f'{where}.{key} is required')
    for key, kind in known.items():
        if key in prof:
            check_type(where, key, prof[key], kind, errors)
    if 'allowed' in prof and prof['allowed'] not in ALLOWED:
        errors.append(f'{where}.allowed must be one of {", ".join(ALLOWED)}')
    if isinstance(prof.get('source'), list) and (not prof['source'] or
                                                 not all(isinstance(s, str) and s for s in prof['source'])):
        errors.append(f'{where}.source must list at least one non-empty string')
    if isinstance(prof.get('arch'), str) and prof['arch'] not in ARCHES:
        errors.append(f'{where}.arch must be one of {", ".join(ARCHES)}')
    if isinstance(prof.get('gpu_type'), str) and not (prof['gpu_type'].isascii() and prof['gpu_type'].isalnum()
                                                      and prof['gpu_type'].islower()):
        errors.append(f'{where}.gpu_type must be lower-case letters and digits')
    if isinstance(prof.get('gpus_per_node'), int) and prof['gpus_per_node'] < 1:
        errors.append(f'{where}.gpus_per_node must be at least 1')
    taint = prof.get('gpu_taint')
    if isinstance(taint, dict):
        for key in taint:
            if key not in ('key', 'value', 'effect'):
                errors.append(f'{where}.gpu_taint.{key} is not a known key')
        for key in ('key', 'effect'):
            if not isinstance(taint.get(key), str) or not taint.get(key):
                errors.append(f'{where}.gpu_taint.{key} is required')
        if 'value' in taint and not isinstance(taint['value'], str):
            errors.append(f'{where}.gpu_taint.value must be a str')
        if isinstance(taint.get('effect'), str) and taint['effect'] not in EFFECTS:
            errors.append(f'{where}.gpu_taint.effect must be one of {", ".join(EFFECTS)}')


def load_profiles():
    path = profiles_path()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ProfileError('profiles_missing', f'no profiles file at {path}; write it from the runbook (profiles.md)')
    except tomllib.TOMLDecodeError as e:
        raise ProfileError('profiles_invalid', f'{path} is not valid TOML: {e}')
    errors = []
    for key in data:
        if key not in ('schema_version', 'bot', 'clusters'):
            errors.append(f'{key} is not a known top-level key')
    # type() and not isinstance(): TOML 1.0 is a float and true is a bool, and both compare equal to 1.
    if type(data.get('schema_version')) is not int or data['schema_version'] != SCHEMA_VERSION:
        errors.append(f'schema_version must be {SCHEMA_VERSION}')
    bot = data.get('bot')
    if not isinstance(bot, dict):
        errors.append('[bot] is required')
        bot = {}
    for key in bot:
        if key not in BOT_KEYS:
            errors.append(f'bot.{key} is not a known key')
    for key, kind in BOT_KEYS.items():
        if key not in bot:
            errors.append(f'bot.{key} is required')
        else:
            check_type('bot', key, bot[key], kind, errors)
    prefix = bot.get('namespace_prefix')
    if isinstance(prefix, str) and prefix and not prefix.endswith('-'):
        errors.append('bot.namespace_prefix must end in "-"')
    kubeconfig = bot.get('kubeconfig')
    problem = bot_kubeconfig_problem(kubeconfig) if isinstance(kubeconfig, str) and kubeconfig else None
    if problem:
        errors.append(problem)
    clusters = data.get('clusters')
    if not isinstance(clusters, dict) or not clusters:
        errors.append('[clusters.<name>] needs at least one cluster')
        clusters = {}
    for name, prof in clusters.items():
        validate_cluster(name, prof, errors)
    if errors:
        raise ProfileError('profiles_invalid', f'{path}: ' + '; '.join(errors))
    bot = dict(bot, kubeconfig=os.path.expanduser(bot['kubeconfig']))
    return {'schema_version': SCHEMA_VERSION, 'bot': bot, 'clusters': clusters}
