"""The cluster profiles file of the k8s-clusters skill: load it and validate it.

The schema is references/profiles.md. Shared by k8s-check and k8s-guard, so both
refuse the same bad file the same way. Standard library only.
"""
import os
from pathlib import Path
import pwd
import shutil
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


def account_home():
    """The account's home from the password database. Unlike $HOME, the caller cannot change it."""
    return pwd.getpwuid(os.getuid()).pw_dir


def resolve_bot_kubeconfig(path):
    """Return (resolved path, None) when path may be the bot's kubeconfig, else (None, reason).

    The rule is an allowlist: after every symlink is resolved, the file must lie under
    tbot's own directory in the account's home, where the tbot config writes it.
    Denylists of path forms kept leaking: a symlinked ~/.kube, a changed HOME, a
    symlink before "..". A leading ~ also expands to the account's home, not $HOME.
    A second check refuses any .kube or .tsh directory in the path as written or as
    resolved, and anything inside the resolved ~/.kube or ~/.tsh of $HOME or the
    account's home: those hold the owner's own login.
    Callers pass the resolved path to kubectl, so a symlink swapped after the check
    is not followed.
    """
    home = account_home()
    if path == '~' or path.startswith('~/'):
        expanded = home + path[1:]
    elif path.startswith('~'):
        return None, f'bot.kubeconfig {path}: only ~/ is expanded, as the account\'s home'
    else:
        expanded = path
    if not os.path.isabs(expanded):
        return None, f'bot.kubeconfig {path} must be an absolute path or start with ~/'
    # realpath resolves each symlink before the ".." after it, as the kernel does.
    real = Path(os.path.realpath(expanded))
    if any(part in OWNER_DIRS for p in (Path(expanded), real) for part in p.parts):
        return None, f'bot.kubeconfig {path} is in a .kube or .tsh directory, the owner\'s login'
    for h in {home, os.path.expanduser('~')}:
        for d in OWNER_DIRS:
            owner = Path(os.path.realpath(os.path.join(h, d)))
            if real == owner or owner in real.parents:
                return None, f'bot.kubeconfig {path} resolves into {owner}, the owner\'s login'
    root = Path(os.path.realpath(os.path.join(home, TBOT_DIR)))
    if root not in real.parents:
        return None, f'bot.kubeconfig {path} resolves to {real}, which is not under {root}'
    return str(real), None


# The only credentials a bot kubeconfig user may hold: tbot's exec plugin, or the client
# certificate tbot writes when its exec plugin is off. A token, an auth provider, an
# impersonation or any other exec command (tsh above all) would act as someone else.
BOT_USER_KEYS = {'exec', 'client-certificate', 'client-certificate-data', 'client-key', 'client-key-data'}
# Where tbot is installed (the tbot setup in the dotfiles README): an exec plugin may run only
# the file one of these resolves to. A name alone is not enough: /tmp/tbot can link to tsh.
TBOT_INSTALLS = ('/usr/local/bin/tbot', '~/.local/bin/tbot')


def tbot_binaries():
    """The resolved paths of the installed tbot binaries, with ~ as the account's home."""
    home = account_home()
    found = set()
    for p in TBOT_INSTALLS:
        real = os.path.realpath(home + p[1:] if p.startswith('~/') else p)
        if os.path.isfile(real):
            found.add(real)
    return found


def bot_credential_problem(view, context):
    """Why the context in a `kubectl config view -o json` result does not use bot credentials, or None."""
    ctx = next((c.get('context') or {} for c in view.get('contexts') or [] if c.get('name') == context), None)
    if ctx is None:
        return f'context {context} is not in the bot kubeconfig'
    user = next((u.get('user') or {} for u in view.get('users') or [] if u.get('name') == ctx.get('user')), None)
    if user is None:
        return f'context {context} names user {ctx.get("user")!r}, which the bot kubeconfig does not define'
    extra = sorted(set(user) - BOT_USER_KEYS)
    if extra:
        return f'the user of context {context} holds {", ".join(extra)}; only tbot credentials are allowed'
    if 'exec' in user:
        command = str((user['exec'] or {}).get('command') or '')
        # client-go looks a bare name up on PATH; kubectl runs with the caller's PATH.
        path = command if os.path.isabs(command) else (shutil.which(command) or '') if command else ''
        if not path or os.path.realpath(path) not in tbot_binaries():
            return (f'the user of context {context} runs {command!r}, which is not an installed tbot '
                    f'({", ".join(TBOT_INSTALLS)})')
    if not user:
        return f'the user of context {context} holds no credentials'
    return None


def kubectl_env(environ=None):
    """The environment for kubectl: no KUBECONFIG and no KUBECTL_* from the caller, and kuberc off.

    --kubeconfig already overrides KUBECONFIG; dropping it too leaves one source. A kuberc file
    (~/.kube/kuberc, kubectl 1.33 and later) can add defaults such as --token to every command.
    """
    env = {k: v for k, v in (os.environ if environ is None else environ).items()
           if k != 'KUBECONFIG' and not k.startswith('KUBECTL_')}
    env['KUBECTL_KUBERC'] = 'false'
    return env


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
    resolved = None
    if isinstance(bot.get('kubeconfig'), str) and bot['kubeconfig']:
        resolved, problem = resolve_bot_kubeconfig(bot['kubeconfig'])
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
    # From here on every reader uses the resolved path, the one that was checked.
    bot = dict(bot, kubeconfig=resolved)
    return {'schema_version': SCHEMA_VERSION, 'bot': bot, 'clusters': clusters}
