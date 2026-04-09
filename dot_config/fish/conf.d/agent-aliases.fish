# Agent infrastructure shortcuts
# Provides quick access to worktree management, agent spawning, and zellij sessions

# Agents zellij session (fixed name, quick attach/create)
function agents --description "Attach to agents zellij session"
    zellij attach agents 2>/dev/null
    or zellij --new-session-with-layout agents --session agents
end

# Worktree shortcuts
function ws --description "List worktrees"
    wt ls $argv
end

function wnew --description "New worktree"
    wt new $argv
end

function wrm --description "Remove worktree"
    wt rm $argv
end

function wcd --description "cd into worktree"
    if test (count $argv) -lt 1
        echo "Usage: wcd <name>"
        return 1
    end
    set -l path (wt cd $argv[1])
    and cd $path
end

# Agent shortcuts
function as --description "Spawn agent on worktree"
    agent-spawn $argv
end

function ast --description "Agent status overview"
    agent-status $argv
end

# Claude Code shortcuts
function cc --description "Claude Code"
    claude $argv
end

function ccc --description "Claude Code continue last session"
    claude --continue $argv
end

function ccr --description "Claude Code resume (picker)"
    claude --resume $argv
end

# Codex shortcuts
function cx --description "Codex CLI"
    codex $argv
end

function cxf --description "Codex full-auto mode"
    codex exec --full-auto $argv
end
