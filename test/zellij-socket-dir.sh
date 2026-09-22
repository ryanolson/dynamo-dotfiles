#!/bin/bash
# Assert every login path agrees on one zellij socket directory.
#
# The defect this guards: zellij derives its socket directory from
# XDG_RUNTIME_DIR, which is set only for logins that run pam_systemd. A login
# that set it landed in /run/user/$UID/zellij, a login that did not landed in
# /tmp/zellij-$UID, and `zellij a dynamo` then reached a different server with
# its own panes and scrollback depending on how the machine was reached.
#
# Runs against the DEPLOYED files in $HOME, so run it after `chezmoi apply`.

set -uo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
fails=0
uid="$(id -u)"
helper="$HOME/.local/bin/zellij-socket-dir"

ok()   { echo -e "${GREEN}[PASS]${NC} $1"; }
bad()  { echo -e "${RED}[FAIL]${NC} $1"; fails=$((fails + 1)); }

check_eq() {
    local what="$1" got="$2" want="$3"
    if [ "$got" = "$want" ]; then ok "$what"; else bad "$what: got '$got', want '$want'"; fi
}

# The rule itself, evaluated under both login shapes. This is the regression:
# these two must not diverge.
if [ ! -x "$helper" ]; then
    bad "helper $helper is missing or not executable"
    echo "cannot continue without the helper"; exit 1
fi
ok "helper $helper is executable"

with_xdg="$(env XDG_RUNTIME_DIR="/run/user/$uid" "$helper")"
without_xdg="$(env -u XDG_RUNTIME_DIR "$helper")"
bogus_xdg="$(env XDG_RUNTIME_DIR=/nonexistent/runtime "$helper")"
check_eq "helper agrees with and without XDG_RUNTIME_DIR" "$without_xdg" "$with_xdg"
check_eq "helper ignores a bogus XDG_RUNTIME_DIR"         "$bogus_xdg"   "$with_xdg"

expected="$with_xdg"
case "$expected" in
    /*) ok "helper prints an absolute path ($expected)" ;;
    *)  bad "helper printed a non-absolute path: '$expected'" ;;
esac

# Interactive shell path: fish conf.d drop-in.
if command -v fish >/dev/null 2>&1; then
    f_with="$(env XDG_RUNTIME_DIR="/run/user/$uid" fish -l -c 'echo $ZELLIJ_SOCKET_DIR' 2>/dev/null | tail -1)"
    f_without="$(env -u XDG_RUNTIME_DIR fish -l -c 'echo $ZELLIJ_SOCKET_DIR' 2>/dev/null | tail -1)"
    check_eq "fish login shell exports the pinned dir"   "$f_with"    "$expected"
    check_eq "fish agrees with XDG_RUNTIME_DIR unset"    "$f_without" "$expected"

    # A shell inside a zellij pane must keep its server's directory.
    nested="$(env ZELLIJ_SOCKET_DIR=/tmp/pinned-by-parent fish -l -c 'echo $ZELLIJ_SOCKET_DIR' 2>/dev/null | tail -1)"
    check_eq "fish preserves an inherited ZELLIJ_SOCKET_DIR" "$nested" "/tmp/pinned-by-parent"
else
    bad "fish not installed; cannot test the fish path"
fi

# POSIX login shell path: ~/.profile.
s_with="$(env XDG_RUNTIME_DIR="/run/user/$uid" sh -lc 'echo $ZELLIJ_SOCKET_DIR' 2>/dev/null | tail -1)"
s_without="$(env -u XDG_RUNTIME_DIR sh -lc 'echo $ZELLIJ_SOCKET_DIR' 2>/dev/null | tail -1)"
check_eq "sh login shell exports the pinned dir" "$s_with"    "$expected"
check_eq "sh agrees with XDG_RUNTIME_DIR unset"  "$s_without" "$expected"

# Non-interactive SSH path: scripts that source no shell rc must resolve it
# themselves, so assert each one wires to the single definition.
for script in "$HOME/.local/bin/dynamo-remote-session" "$HOME/.local/bin/zellij-wrapper"; do
    name="$(basename "$script")"
    if [ ! -f "$script" ]; then
        bad "$name is not deployed"
    elif grep -q 'zellij-socket-dir' "$script"; then
        ok "$name resolves the socket dir from the shared helper"
    else
        bad "$name never sets ZELLIJ_SOCKET_DIR; non-interactive SSH will split sessions"
    fi
done

echo
if [ "$fails" -eq 0 ]; then
    echo -e "${GREEN}all checks passed${NC} (socket dir: $expected)"; exit 0
else
    echo -e "${RED}$fails check(s) failed${NC}"; exit 1
fi
