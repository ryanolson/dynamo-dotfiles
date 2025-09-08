#!/bin/bash
set -euo pipefail

# Cursor Tunnel Launch Script with Reference Counting
# Manages a shared cursor tunnel across multiple claude sessions
# Uses hard links for reference counting - tunnel closes when last session exits

# Configuration
CURSOR_CLI="${CURSOR_CLI:-cursor}"
CLAUDE_COMMAND="${CLAUDE_COMMAND:-claude}"
TUNNEL_DIR="${HOME}/.local/share/cursor-tunnels"
LOCK_DIR="$TUNNEL_DIR/locks"
LOG_DIR="$TUNNEL_DIR/logs"

# Files for tunnel management
MASTER_LOCK="$LOCK_DIR/tunnel.lock"
NAME_FILE="$LOCK_DIR/tunnel.name"
SESSION_LOCK="$LOCK_DIR/session.$$"

# Global state
TUNNEL_LOG=""

# Setup logging
setup_logging() {
    mkdir -p "$LOG_DIR"
    local timestamp=$(date +%Y%m%d-%H%M%S)
    TUNNEL_LOG="$LOG_DIR/tunnel-${timestamp}.log"
    echo "[$(date)] Session $$ starting" >> "$TUNNEL_LOG"
}

# Generate tunnel name from context
generate_tunnel_name() {
    local branch_name=""
    local project_name=""
    
    # Get branch name from ccmanager or git
    if [ -n "${CCMANAGER_BRANCH_NAME:-}" ]; then
        branch_name="${CCMANAGER_BRANCH_NAME}"
    else
        # Fallback to git detection
        if git rev-parse --git-dir > /dev/null 2>&1; then
            branch_name=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
        else
            branch_name="main"
        fi
    fi
    
    # Get project name
    if [ -n "${CCMANAGER_PROJECT:-}" ]; then
        # Use ccmanager project if available
        project_name="${CCMANAGER_PROJECT}"
    elif [ -n "${CCMANAGER_WORKTREE_PATH:-}" ]; then
        # Extract from worktree path pattern: ../{project}-workspaces/{branch}
        # The parent directory is like "dynamo-workspaces", we want "dynamo"
        local workspaces_dir=$(dirname "$CCMANAGER_WORKTREE_PATH")
        local workspaces_name=$(basename "$workspaces_dir")
        # Remove the -workspaces suffix to get the project name
        project_name="${workspaces_name%-workspaces}"
    elif git rev-parse --git-dir > /dev/null 2>&1; then
        # Fallback to git repo name
        project_name=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)")
    else
        # Last resort: current directory
        project_name=$(basename "$(pwd)")
    fi
    
    # Clean names for tunnel identifier (replace non-alphanumeric with hyphens)
    project_name=${project_name//[^a-zA-Z0-9]/-}
    branch_name=${branch_name//[^a-zA-Z0-9]/-}
    
    # Combine project and branch
    local tunnel_name="${project_name}-${branch_name}"
    
    # If too long, truncate and add hash for uniqueness
    if [ ${#tunnel_name} -gt 50 ]; then
        # Generate 6-char hash of full combined name
        local hash=$(echo "${project_name}-${branch_name}" | sha256sum | cut -c1-6)
        # Truncate to leave room for "-" and 6-char hash (50 - 7 = 43)
        tunnel_name="${tunnel_name:0:43}-${hash}"
    fi
    
    echo "$tunnel_name"
}

# Start the actual tunnel process
start_tunnel_process() {
    local tunnel_name="$1"
    
    echo "[$(date)] Starting tunnel process: $tunnel_name" >> "$TUNNEL_LOG"
    
    # Check if Cursor CLI exists
    if ! command -v "$CURSOR_CLI" &> /dev/null; then
        echo "[$(date)] ERROR: Cursor CLI not found" >> "$TUNNEL_LOG"
        echo "Error: Cursor CLI not found. Please install it first." >&2
        return 1
    fi
    
    # Set up authentication if needed
    if ! "$CURSOR_CLI" tunnel user show &>/dev/null; then
        echo "[$(date)] Authenticating with GitHub..." >> "$TUNNEL_LOG"
        CURSOR_CLI_DISABLE_KEYCHAIN_ENCRYPT=1 \
            "$CURSOR_CLI" tunnel user login --provider github
    fi
    
    # Launch tunnel
    "$CURSOR_CLI" tunnel \
        --name "$tunnel_name" \
        --accept-server-license-terms 2>&1 | \
        while IFS= read -r line; do
            echo "[CURSOR] $line" >> "$TUNNEL_LOG"
            
            # Show tunnel URL only once when it starts
            if [[ "$line" == *"https://"* ]] && [ ! -f "$LOCK_DIR/url_shown" ]; then
                echo "🔗 Cursor tunnel available at: $(echo "$line" | grep -oE 'https://[^ ]+' | head -1)"
                touch "$LOCK_DIR/url_shown"
            fi
        done
}

# Acquire tunnel (start new or join existing)
acquire_tunnel() {
    mkdir -p "$LOCK_DIR"
    
    echo "[$(date)] Acquiring tunnel lock..." >> "$TUNNEL_LOG"
    
    # Use flock for atomic operations
    (
        flock -x 200
        
        # Check if tunnel is already running
        if [ -f "$MASTER_LOCK" ]; then
            local tunnel_pid=$(cat "$MASTER_LOCK")
            if kill -0 "$tunnel_pid" 2>/dev/null; then
                # Tunnel is running, create hard link (increment ref count)
                ln "$MASTER_LOCK" "$SESSION_LOCK"
                local links=$(stat -c %h "$MASTER_LOCK" 2>/dev/null || stat -f %l "$MASTER_LOCK" 2>/dev/null)
                echo "[$(date)] Joined existing tunnel (PID: $tunnel_pid, sessions: $links)" >> "$TUNNEL_LOG"
                
                # Get the existing tunnel name
                if [ -f "$NAME_FILE" ]; then
                    local tunnel_name=$(cat "$NAME_FILE")
                    echo "♻️  Reusing existing tunnel: $tunnel_name (sessions: $links)"
                fi
                return 0
            else
                # Stale lock file, clean up
                echo "[$(date)] Cleaning stale lock (PID $tunnel_pid not running)" >> "$TUNNEL_LOG"
                rm -f "$MASTER_LOCK" "$NAME_FILE" "$LOCK_DIR/url_shown"
            fi
        fi
        
        # Need to start new tunnel
        local tunnel_name=$(generate_tunnel_name)
        echo "[$(date)] Starting new tunnel: $tunnel_name" >> "$TUNNEL_LOG"
        echo "🏷️  Tunnel name: $tunnel_name"
        echo "🚀 Starting new shared cursor tunnel..."
        
        # Start tunnel in background
        start_tunnel_process "$tunnel_name" &
        local tunnel_pid=$!
        
        # Save tunnel info
        echo "$tunnel_pid" > "$MASTER_LOCK"
        echo "$tunnel_name" > "$NAME_FILE"
        
        # Create our session link
        ln "$MASTER_LOCK" "$SESSION_LOCK"
        
        echo "[$(date)] Tunnel started with PID: $tunnel_pid" >> "$TUNNEL_LOG"
        echo "✅ Cursor tunnel running (PID: $tunnel_pid)"
        
        # Give tunnel time to establish
        sleep 3
        
    ) 200>"$LOCK_DIR/.flock"
}

# Release tunnel (decrement refs or stop if last)
release_tunnel() {
    echo "[$(date)] Releasing tunnel lock..." >> "$TUNNEL_LOG"
    
    (
        flock -x 200
        
        if [ -f "$SESSION_LOCK" ]; then
            # Get link count before removing our link
            local links=$(stat -c %h "$SESSION_LOCK" 2>/dev/null || stat -f %l "$SESSION_LOCK" 2>/dev/null)
            echo "[$(date)] Current sessions: $links" >> "$TUNNEL_LOG"
            
            # Remove our session link
            rm -f "$SESSION_LOCK"
            
            if [ $links -le 2 ]; then  # Was 2 (master + ours), now just master
                # We were the last session
                if [ -f "$MASTER_LOCK" ]; then
                    local tunnel_pid=$(cat "$MASTER_LOCK")
                    echo "[$(date)] Last session, stopping tunnel PID: $tunnel_pid" >> "$TUNNEL_LOG"
                    echo "🔚 Last session, closing tunnel..."
                    
                    # Kill tunnel process
                    kill -TERM "$tunnel_pid" 2>/dev/null || true
                    
                    # Give it time to cleanup
                    local wait_count=0
                    while kill -0 "$tunnel_pid" 2>/dev/null && [ $wait_count -lt 5 ]; do
                        sleep 1
                        ((wait_count++))
                    done
                    
                    # Force kill if necessary
                    if kill -0 "$tunnel_pid" 2>/dev/null; then
                        kill -KILL "$tunnel_pid" 2>/dev/null || true
                    fi
                    
                    # Clean up all files
                    rm -f "$MASTER_LOCK" "$NAME_FILE" "$LOCK_DIR/url_shown"
                fi
            else
                echo "[$(date)] Tunnel still in use ($((links - 1)) sessions remaining)" >> "$TUNNEL_LOG"
                echo "📉 Tunnel still in use ($((links - 1)) sessions)"
            fi
        else
            echo "[$(date)] No session lock found" >> "$TUNNEL_LOG"
        fi
        
    ) 200>"$LOCK_DIR/.flock"
}

# Launch Claude with provided arguments
launch_claude() {
    local claude_args=("$@")
    
    # Change to worktree directory if available
    if [ -n "${CCMANAGER_WORKTREE_PATH:-}" ]; then
        cd "$CCMANAGER_WORKTREE_PATH"
        echo "[$(date)] Changed to worktree: $CCMANAGER_WORKTREE_PATH" >> "$TUNNEL_LOG"
        echo "📂 Working directory: $CCMANAGER_WORKTREE_PATH"
    else
        echo "📁 Working directory: $(pwd)"
    fi
    
    echo "[$(date)] Launching Claude with args: ${claude_args[*]}" >> "$TUNNEL_LOG"
    
    # Launch Claude in foreground (needs stdin for interactive mode)
    "$CLAUDE_COMMAND" "${claude_args[@]}"
    local claude_exit_code=$?
    
    echo "[$(date)] Claude exited with code: $claude_exit_code" >> "$TUNNEL_LOG"
    return $claude_exit_code
}

# Cleanup function
cleanup() {
    echo "[$(date)] Cleanup initiated" >> "$TUNNEL_LOG"
    release_tunnel
    echo "[$(date)] Cleanup completed" >> "$TUNNEL_LOG"
}

# Main execution
main() {
    # Setup
    setup_logging
    
    # Set up signal handlers
    trap cleanup EXIT SIGINT SIGTERM
    
    # Acquire tunnel (start new or join existing)
    acquire_tunnel
    
    # Launch Claude with all arguments
    launch_claude "$@"
}

# Execute main function with all script arguments
main "$@"