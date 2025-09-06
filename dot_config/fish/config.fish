# Fish shell configuration - Dynamo team setup
# This config provides a modern, productive shell environment

# Handle unsupported TERM types (e.g., ghostty)
if test "$TERM" = "xterm-ghostty"
    set -gx TERM xterm-256color
end

# Set up colors and environment
set -gx COLORTERM truecolor
set -gx EDITOR hx
set -gx VISUAL hx

# Add local bin directories to PATH
fish_add_path $HOME/.local/bin
fish_add_path $HOME/.cargo/bin  # Rust tools
fish_add_path $HOME/.npm-global/bin  # npm global packages

# Configure npm to use user directory for global packages
set -gx NPM_CONFIG_PREFIX $HOME/.npm-global
mkdir -p $HOME/.npm-global

# Initialize tool integrations
if command -v zoxide >/dev/null
    zoxide init fish | source
end

if command -v starship >/dev/null
    starship init fish | source
end

if command -v mise >/dev/null
    mise activate fish | source
end

# Install npm tools for AI development on first run
if command -v npm >/dev/null
    # Check and install ccmanager (Claude Code session manager)
    if not command -v ccmanager >/dev/null
        echo "🔧 Installing ccmanager (Claude Code session manager)..."
        npm install -g ccmanager >/dev/null 2>&1
    end
    
    # Check and install Claude Code CLI
    if not command -v claude >/dev/null
        echo "🔧 Installing Claude Code CLI..."
        npm install -g @anthropic-ai/claude-code >/dev/null 2>&1
    end
    
    # Check and install ruler (AI agent configuration manager)
    if not command -v ruler >/dev/null
        echo "🔧 Installing ruler (AI agent configuration manager)..."
        npm install -g @intellectronica/ruler >/dev/null 2>&1
    end
end

# Team-standard aliases
alias cat "bat"
alias ls "eza"
alias l "eza"
alias tree "broot"
alias grep "rg"
alias find "fd"
alias c "z"  # zoxide jump

# Editor shortcuts
alias h "hx"  # helix

# Git shortcuts (common team patterns)
alias gst "git status"
alias gco "git checkout"
alias gp "git push"
alias gl "git pull"
alias ga "git add"
alias gc "git commit"

# Development shortcuts
alias k "kubectl"  # kubernetes
alias d "docker"   # docker

# Useful functions
function mkcd --description "Create directory and cd into it"
    mkdir -p $argv[1] && cd $argv[1]
end

function backup --description "Create a backup of a file"
    cp $argv[1] $argv[1].backup.(date +%Y%m%d_%H%M%S)
end

function extract --description "Extract various archive formats"
    if test -f $argv[1]
        switch $argv[1]
            case "*.tar.bz2"
                tar xjf $argv[1]
            case "*.tar.gz"
                tar xzf $argv[1]
            case "*.bz2"
                bunzip2 $argv[1]
            case "*.rar"
                unrar e $argv[1]
            case "*.gz"
                gunzip $argv[1]
            case "*.tar"
                tar xf $argv[1]
            case "*.tbz2"
                tar xjf $argv[1]
            case "*.tgz"
                tar xzf $argv[1]
            case "*.zip"
                unzip $argv[1]
            case "*.Z"
                uncompress $argv[1]
            case "*.7z"
                7z x $argv[1]
            case "*"
                echo "Unknown archive format: $argv[1]"
        end
    else
        echo "File not found: $argv[1]"
    end
end

# Git shortcuts function
function gcom --description "Git commit with message"
    git commit -m $argv
end

function gpom --description "Git push origin main"
    git push origin main
end

function gst --description "Enhanced git status"
    git status --short --branch
end

# Welcome message for new shells
function fish_greeting
    set_color $fish_color_autosuggestion
    echo "Welcome to Dynamo development environment"
    echo "   Editor: helix | Shell: fish | Prompt: starship"
    if command -v mise >/dev/null
        echo "   Runtime manager: mise ("(mise ls --current 2>/dev/null | wc -l | string trim)" runtimes active)"
    end
    set_color normal
end