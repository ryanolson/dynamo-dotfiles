# Point the standalone bb CLI at the shared bb server. chezmoi writes the URL to
# ~/.config/bb/server-url from the tailnet suffix.
#
# Left alone when already set: bb sets BB_SERVER_URL in its own thread shells,
# and a deliberate override must win.
if not set -q BB_SERVER_URL; and test -s $HOME/.config/bb/server-url
    read -l _bb_url <$HOME/.config/bb/server-url
    and set -gx BB_SERVER_URL $_bb_url
end
