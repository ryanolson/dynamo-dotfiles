#!/usr/bin/env bash
# Run from the command that `fleet invite` prints. Keep the token out of shell traces.
set +x
set -euo pipefail
umask 077

fail() { printf 'join: %s\n' "$*" >&2; exit 1; }

repo="" ref="" name="" ca_url="" fingerprint="" token="" dotfiles=0 ephemeral=0
fleet_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo) repo="${2:-}"; shift 2 ;;
        --ref) ref="${2:-}"; shift 2 ;;
        --name) name="${2:-}"; shift 2 ;;
        --ca-url) ca_url="${2:-}"; shift 2 ;;
        --fingerprint) fingerprint="${2:-}"; shift 2 ;;
        --token) token="${2:-}"; shift 2 ;;
        --dotfiles) dotfiles=1; shift ;;
        --ephemeral) ephemeral=1; shift ;;
        --no-trust) fleet_args+=(--no-trust); shift ;;
        --principal) fleet_args+=(--principal "${2:-}"); shift 2 ;;
        *) fail "unknown option $1" ;;
    esac
done
[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail 'invalid --repo'
# A full commit SHA pins the content of every file that this script downloads.
[[ "$ref" =~ ^[0-9a-f]{40}$ ]] || fail 'invalid --ref (full commit SHA)'
[[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$ ]] || fail 'invalid --name'
[[ "$ca_url" =~ ^https:// ]] || fail 'invalid --ca-url'
[[ "$fingerprint" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid --fingerprint'
[[ "$token" =~ ^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$ ]] || fail 'invalid --token'
command -v curl >/dev/null || fail 'curl is required'

raw="https://raw.githubusercontent.com/$repo/$ref"
work=$(mktemp -d "${TMPDIR:-/tmp}/fleet-join.XXXXXX")
trap 'rm -rf "$work"' EXIT

if [[ "$dotfiles" == 1 ]]; then
    curl -fsSL -o "$work/bootstrap.sh" "$raw/bootstrap.sh" || fail 'could not download bootstrap.sh'
    bash "$work/bootstrap.sh"
fi

fleet="$HOME/.local/bin/fleet"
if [[ "$ephemeral" == 1 || ! -x "$fleet" ]]; then
    curl -fsSL -o "$work/fleet" "$raw/dot_local/bin/executable_fleet" || fail 'could not download fleet'
    chmod 700 "$work/fleet"
    if [[ "$ephemeral" == 1 ]]; then
        fleet="$work/fleet"
    else
        mkdir -p "$HOME/.local/bin"
        install -m 0755 "$work/fleet" "$fleet"
    fi
fi

if [[ "$ephemeral" == 1 ]]; then
    printf '%s\n' "$token" | "$fleet" ephemeral "$name" --ca-url "$ca_url" --fingerprint "$fingerprint" \
        "${fleet_args[@]+"${fleet_args[@]}"}"
else
    printf '%s\n' "$token" | "$fleet" enroll "$name" --ca-url "$ca_url" --fingerprint "$fingerprint" \
        "${fleet_args[@]+"${fleet_args[@]}"}"
    "$fleet" timer install
fi
