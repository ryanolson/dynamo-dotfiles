# Pin zellij's IPC socket directory so every login path sees the same sessions.
#
# Without this, the directory follows XDG_RUNTIME_DIR, which is set only for
# logins that run pam_systemd. Reaching this host two different ways then gives
# `zellij a dynamo` two different servers. See ~/.local/bin/zellij-socket-dir
# for which directory is chosen and why.
#
# Left alone when already set, so a shell started inside a zellij pane keeps the
# directory its server was launched with.
#
# conf.d runs before config.fish builds PATH, hence the absolute path.
if not set -q ZELLIJ_SOCKET_DIR
    if test -x $HOME/.local/bin/zellij-socket-dir
        set -gx ZELLIJ_SOCKET_DIR ($HOME/.local/bin/zellij-socket-dir)
    else
        # Helper missing (fresh machine, mid-bootstrap). Inline the same rule
        # rather than silently falling back to the split behaviour.
        set -l _uid (id -u)
        if test -d /run/user/$_uid -a -w /run/user/$_uid
            set -gx ZELLIJ_SOCKET_DIR /run/user/$_uid/zellij
        else
            set -gx ZELLIJ_SOCKET_DIR /tmp/zellij-$_uid
        end
    end
end
