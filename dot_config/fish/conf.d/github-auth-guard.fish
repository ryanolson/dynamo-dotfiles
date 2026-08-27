# GitHub auth policy: `gh auth login`, never a personal access token.
#
# `gh` prefers GH_TOKEN / GITHUB_TOKEN over its own stored credential, so a stray
# export silently overrides the authenticated session -- and a token that lands in
# a shell profile outlives the reason it was added. Erase them here and let `gh`
# fall through to the credential it manages itself.
#
# Interactive shells only: a bash CI job that deliberately exports one is untouched.

if status is-interactive
    set -l _gh_token_vars GITHUB_TOKEN GH_TOKEN GITHUB_PAT GH_PAT \
        GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN

    for _var in $_gh_token_vars
        if set -q $_var; and test -n "$$_var"
            set_color yellow
            echo "warning: $_var was set in the environment; erasing it."
            echo "         GitHub auth is `gh auth login` only -- never a PAT."
            set_color normal
            set -e $_var
        end
    end
    set -e _var
end
