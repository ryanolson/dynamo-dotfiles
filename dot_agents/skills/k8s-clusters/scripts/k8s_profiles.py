"""The cluster profiles file of the k8s-clusters skill: load it and validate it.

The schema is references/profiles.md. Shared by k8s-check and k8s-guard, so both
refuse the same bad file the same way. Standard library only.
"""
import fcntl
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import tomllib
import urllib.parse

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


TBOT_INSTALLS = ('/usr/local/bin/tbot', '~/.local/bin/tbot')
# The one exec shape tbot v18.7.6 writes (lib/tbot/services/k8s/output_v2.go,
# generateKubeConfigV2WithPlugin), as `kubectl config view -o json` prints it: kubectl adds
# env null, provideClusterInfo false and interactiveMode IfAvailable. Widen only from a real
# tbot kubeconfig, never by guess: `tbot proxy`, for one, hands off to tsh.
TBOT_EXEC_API = 'client.authentication.k8s.io/v1beta1'
TBOT_EXEC_DEFAULTS = {'env': (None, []), 'provideClusterInfo': (None, False),
                      'interactiveMode': (None, 'IfAvailable'), 'installHint': (None, '')}
TBOT_DESTINATION_FLAG = '--destination-dir='


def tbot_binaries():
    """(st_dev, st_ino) of each installed tbot, following symlinks, with ~ as the account's home.

    File identity, not a path string: tbot may write either the install path or the
    file it links to (on one host /usr/local/bin/tbot links into /opt/teleport).
    """
    home = account_home()
    found = set()
    for p in TBOT_INSTALLS:
        try:
            st = os.stat(home + p[1:] if p.startswith('~/') else p)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            found.add((st.st_dev, st.st_ino))
    return found


def tbot_exec_problem(plugin, kubeconfig):
    """Why an exec plugin is not exactly tbot's `kube credentials` for this kubeconfig, or None.

    kubeconfig is the resolved bot kubeconfig. tbot writes it into the same directory it
    names in --destination-dir, so that flag must resolve to the kubeconfig's directory.
    """
    if not isinstance(plugin, dict):
        return 'exec is not a table'
    extra = sorted(set(plugin) - {'command', 'args', 'apiVersion', *TBOT_EXEC_DEFAULTS})
    if extra:
        return f'exec holds {", ".join(extra)}'
    command = plugin.get('command')
    # An absolute path only: a bare name goes through PATH, and Go and Python resolve
    # PATH entries such as current/../bin differently. No "." or ".." either.
    if not isinstance(command, str) or not os.path.isabs(command) or \
            any(part in ('.', '..') for part in command.split('/')):
        return f'exec runs {command!r}; it must be an absolute path to an installed tbot, without . or ..'
    try:
        st = os.stat(command)
    except OSError:
        return f'exec runs {command!r}, which does not exist'
    if (st.st_dev, st.st_ino) not in tbot_binaries():
        return f'exec runs {command!r}, which is not an installed tbot ({", ".join(TBOT_INSTALLS)})'
    if plugin.get('apiVersion') != TBOT_EXEC_API:
        return f'exec apiVersion is {plugin.get("apiVersion")!r}, not {TBOT_EXEC_API}'
    for key, allowed in TBOT_EXEC_DEFAULTS.items():
        if plugin.get(key) not in allowed:
            return f'exec sets {key} to {plugin.get(key)!r}; tbot writes none'
    args = plugin.get('args')
    shape = f'["kube", "credentials", "{TBOT_DESTINATION_FLAG}<the kubeconfig\'s directory>"]'
    if not isinstance(args, list) or len(args) != 3 or args[:2] != ['kube', 'credentials'] or \
            not isinstance(args[2], str) or not args[2].startswith(TBOT_DESTINATION_FLAG):
        return f'exec args are {args!r}; only {shape} is allowed'
    destination = args[2][len(TBOT_DESTINATION_FLAG):]
    # tbot's Go joins and cleans the destination before the kernel follows symlinks, so
    # current/.. may name another directory to tbot than to realpath. Allow no . or .. at all.
    if not os.path.isabs(destination) or any(part in ('.', '..') for part in destination.split('/')):
        return f'exec destination {destination!r} must be an absolute path without . or ..'
    if os.path.realpath(destination) != os.path.dirname(os.path.realpath(kubeconfig)):
        return f'exec destination {destination!r} is not the directory of the bot kubeconfig; only {shape} is allowed'
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


KUBECONFIG_MAX = 1 << 20


def sealed_bytes(data):
    """Put data in a memfd sealed against any change, and return its inheritable descriptor.

    kubectl reads it through /proc/self/fd/<n>. Nothing can change these bytes afterwards:
    the seals stop writes, growth and shrinking, and further seals.
    """
    if not hasattr(os, 'memfd_create') or not os.path.isdir('/proc/self/fd'):
        raise ProfileError('kubeconfig_not_bot', 'this host has no memfd or /proc/self/fd; the check needs Linux')
    fd = os.memfd_create('k8s-clusters-kubeconfig', os.MFD_ALLOW_SEALING)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]
    fcntl.fcntl(fd, fcntl.F_ADD_SEALS,
                fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
    os.set_inheritable(fd, True)
    return fd


def sealed_copy(path):
    """Read the file at path once (no symlink as its last part) into a sealed memfd."""
    try:
        src = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        raise ProfileError('kubeconfig_not_bot', f'cannot open the bot kubeconfig {path}: {e.strerror}')
    try:
        if not stat.S_ISREG(os.fstat(src).st_mode):
            raise ProfileError('kubeconfig_not_bot', f'the bot kubeconfig {path} is not a regular file')
        data = os.read(src, KUBECONFIG_MAX + 1)
    finally:
        os.close(src)
    if len(data) > KUBECONFIG_MAX:
        raise ProfileError('kubeconfig_not_bot', f'the bot kubeconfig {path} is larger than {KUBECONFIG_MAX} bytes')
    return sealed_bytes(data)


# kubectl runs only from these paths (~ is the passwd home), never from PATH, and only as an
# ELF binary: a wrapper script earlier on PATH, or at one of these paths, may run tsh.
KUBECTL_INSTALLS = ('/usr/local/bin/kubectl', '/usr/bin/kubectl', '~/.local/bin/kubectl')


def is_elf(path):
    try:
        with open(path, 'rb') as f:
            return f.read(4) == b'\x7fELF'
    except OSError:
        return False


def kubectl_binary():
    """The resolved path of the first installed kubectl ELF binary in KUBECTL_INSTALLS."""
    home = account_home()
    for p in KUBECTL_INSTALLS:
        real = os.path.realpath(home + p[1:] if p.startswith('~/') else p)
        if os.path.isfile(real) and os.access(real, os.X_OK) and is_elf(real):
            return real
    raise ProfileError('kubectl_not_found', f'no kubectl ELF binary at {", ".join(KUBECTL_INSTALLS)}')


# What tbot v18.7.6 writes for one context (lib/tbot/services/k8s/output_v2.go). The renderer
# copies only these fields into a fresh kubeconfig, so a field it does not copy cannot act.
# A field outside them is refused by name, so a mistake is reported, not silently dropped.
TBOT_CLUSTER_KEYS = {'server', 'tls-server-name', 'certificate-authority-data'}
TBOT_CONTEXT_KEYS = {'cluster', 'user', 'namespace'}
TBOT_CERT_KEYS = {'client-certificate-data', 'client-key-data'}


def _one(view, kind, name):
    entries = [e for e in view.get(kind) or [] if isinstance(e, dict) and e.get('name') == name]
    if len(entries) != 1:
        return None, f'{kind} names {name!r} {len(entries)} times; it must be exactly once'
    body = entries[0].get(kind[:-1])
    if not isinstance(body, dict):
        return None, f'{kind[:-1]} {name!r} is not a table'
    return body, None


def render_bot_kubeconfig(view, context, kubeconfig):
    """Render a fresh kubeconfig for one context from `kubectl config view --raw --flatten -o json`.

    Returns (JSON bytes, None), or (None, why it is not the bot's). kubeconfig is the resolved
    path of the bot kubeconfig the view was read from: tbot's exec plugin names its directory.
    """
    ctx, problem = _one(view, 'contexts', context)
    if problem:
        return None, problem
    extra = sorted(set(ctx) - TBOT_CONTEXT_KEYS)
    if extra:
        return None, f'context {context} holds {", ".join(extra)}'
    cluster, problem = _one(view, 'clusters', ctx.get('cluster'))
    if problem:
        return None, problem
    extra = sorted(set(cluster) - TBOT_CLUSTER_KEYS)
    if extra:
        return None, f'cluster {ctx.get("cluster")!r} holds {", ".join(extra)}'
    server = cluster.get('server')
    try:
        url = urllib.parse.urlsplit(server) if isinstance(server, str) else None
        if url:
            url.port  # raises ValueError for a port that is not a number
    except ValueError:
        url = None
    if not url or url.scheme != 'https' or not url.hostname:
        return None, f'cluster server {server!r} must be an https URL; http skips the bot\'s credentials'
    if url.username is not None or url.password is not None or '@' in url.netloc:
        return None, f'cluster server {server!r} holds userinfo, which would be sent as credentials'
    if not isinstance(cluster.get('certificate-authority-data'), str) or not cluster['certificate-authority-data']:
        return None, f'cluster {ctx.get("cluster")!r} has no certificate-authority-data'
    if 'tls-server-name' in cluster and not isinstance(cluster['tls-server-name'], str):
        return None, f'cluster {ctx.get("cluster")!r} tls-server-name is not a string'
    user, problem = _one(view, 'users', ctx.get('user'))
    if problem:
        return None, problem
    if not user:
        return None, f'user {ctx.get("user")!r} holds no credentials'
    if 'exec' in user:
        extra = sorted(set(user) - {'exec'})
        if extra:
            return None, f'user {ctx.get("user")!r} holds {", ".join(extra)} beside tbot\'s exec plugin'
        problem = tbot_exec_problem(user['exec'], kubeconfig)
        if problem:
            return None, f'user {ctx.get("user")!r}: {problem}'
        rendered_user = {'exec': {k: user['exec'][k] for k in ('apiVersion', 'command', 'args')}}
    elif set(user) == TBOT_CERT_KEYS and all(isinstance(user[k], str) and user[k] for k in TBOT_CERT_KEYS):
        rendered_user = {k: user[k] for k in sorted(TBOT_CERT_KEYS)}
    else:
        return None, (f'user {ctx.get("user")!r} holds {", ".join(sorted(user))}; only tbot\'s exec plugin '
                      f'or embedded client certificate data is allowed')
    rendered_context = {'cluster': ctx['cluster'], 'user': ctx['user']}
    if isinstance(ctx.get('namespace'), str) and ctx['namespace']:
        rendered_context['namespace'] = ctx['namespace']
    rendered = {
        'apiVersion': 'v1', 'kind': 'Config',
        'clusters': [{'name': ctx['cluster'], 'cluster': {k: cluster[k] for k in sorted(TBOT_CLUSTER_KEYS) if k in cluster}}],
        'users': [{'name': ctx['user'], 'user': rendered_user}],
        'contexts': [{'name': context, 'context': rendered_context}],
        'current-context': context,
    }
    return json.dumps(rendered).encode(), None


def prepare_bot_kubeconfig(kubeconfig, context, timeout=60):
    """Return (kubectl path, sealed descriptor of the rendered kubeconfig) for one context.

    kubeconfig is the resolved bot kubeconfig from load_profiles. The file is read once into a
    sealed copy, kubectl reads that copy locally (config view runs no credential plugin), and
    the rendered result goes into a second sealed copy. Every later kubectl call must use the
    returned kubectl path, the returned descriptor through /proc/self/fd/<n>, and kubectl_env().
    Raises ProfileError: kubectl_not_found, kubeconfig_not_bot, kubectl_error, context_missing,
    or kubeconfig_credentials.
    """
    kubectl = kubectl_binary()
    raw = sealed_copy(kubeconfig)
    cmd = [kubectl, '--kubeconfig', f'/proc/self/fd/{raw}', 'config', 'view', '--raw', '--flatten', '-o', 'json']
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                           env=kubectl_env(), pass_fds=(raw,))
    except subprocess.TimeoutExpired:
        raise ProfileError('kubectl_error', f'kubectl config view timed out after {timeout}s')
    finally:
        os.close(raw)
    if p.returncode != 0:
        raise ProfileError('kubectl_error', f'kubectl config view exited {p.returncode}: {p.stderr.strip()[:400]}')
    try:
        view = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise ProfileError('kubectl_error', 'kubectl config view output is not JSON')
    if context not in [c.get('name') for c in view.get('contexts') or [] if isinstance(c, dict)]:
        raise ProfileError('context_missing', f'context {context} is not in the bot kubeconfig')
    data, problem = render_bot_kubeconfig(view, context, kubeconfig)
    if problem:
        raise ProfileError('kubeconfig_credentials', problem)
    return kubectl, sealed_bytes(data)


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
