# Stable SSH_AUTH_SOCK for persistent sessions (zellij)
# Problem: SSH sets SSH_AUTH_SOCK to /tmp/ssh-*/agent.* which dies when SSH disconnects.
#          Persistent zellij sessions then lose access to the agent.
# Solution: On SSH login, symlink real socket to a stable path. All shells use the stable path.

if status is-login; and set -q SSH_CONNECTION
    # Fresh SSH login -- update the stable symlink to the real socket
    if set -q SSH_AUTH_SOCK; and test "$SSH_AUTH_SOCK" != "$HOME/.ssh/auth_sock"
        ln -sf "$SSH_AUTH_SOCK" "$HOME/.ssh/auth_sock"
    end
end

# Always use the stable path (so zellij sessions survive SSH reconnects)
if test -e "$HOME/.ssh/auth_sock"
    set -gx SSH_AUTH_SOCK "$HOME/.ssh/auth_sock"
end
