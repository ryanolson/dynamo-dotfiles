#!/bin/bash
set -euo pipefail

# Cursor Tunnel Launch Script
# Manages cursor tunnel lifecycle alongside claude sessions
# Accepts claude arguments and passes them through

# Configuration
CURSOR_CLI="${CURSOR_CLI:-cursor}"
CLAUDE_COMMAND="${CLAUDE_COMMAND:-claude}"
TUNNEL_PREFIX="${TUNNEL_PREFIX:-dev}"
LOG_DIR="${HOME}/.local/share/cursor-tunnels"

# Global state
CURSOR_TUNNEL_PID=""
CLAUDE_PID=""
TUNNEL_NAME=""
TUNNEL_LOG=""

# Setup logging
setup_logging() {
    mkdir -p "$LOG_DIR"
    local timestamp=$(date +%Y%m%d-%H%M%S)
    TUNNEL_LOG="$LOG_DIR/tunnel-${timestamp}.log"
    echo "[$(date)] Starting cursor tunnel session" >> "$TUNNEL_LOG"
}

# Generate tunnel name from context
generate_tunnel_name() {
    local project_name=""
    local branch_name=""
    
    # Use ccmanager environment variables if available
    if [ -n "${CCMANAGER_WORKTREE_PATH:-}" ]; then
        project_name=$(basename "$(dirname "$CCMANAGER_WORKTREE_PATH")")
        branch_name="${CCMANAGER_BRANCH_NAME:-main}"
    else
        # Fallback to git detection
        if git rev-parse --git-dir > /dev/null 2>&1; then
            project_name=$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")
            branch_name=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
        else
            project_name=$(basename "$(pwd)")
            branch_name="main"
        fi
    fi
    
    # Clean names for tunnel identifier
    project_name=${project_name//[^a-zA-Z0-9]/-}
    branch_name=${branch_name//[^a-zA-Z0-9]/-}
    
    # Generate unique tunnel name
    TUNNEL_NAME="${TUNNEL_PREFIX}-${project_name}-${branch_name}"
    
    # Truncate if too long (tunnel names have length limits)
    if [ ${#TUNNEL_NAME} -gt 60 ]; then
        TUNNEL_NAME="${TUNNEL_NAME:0:60}"
    fi
    
    echo "[$(date)] Generated tunnel name: $TUNNEL_NAME" >> "$TUNNEL_LOG"
}

# Launch Cursor tunnel
launch_cursor_tunnel() {
    echo "[$(date)] Launching Cursor tunnel: $TUNNEL_NAME" >> "$TUNNEL_LOG"
    
    # Check if Cursor CLI exists
    if ! command -v "$CURSOR_CLI" &> /dev/null; then
        echo "[$(date)] ERROR: Cursor CLI not found at: $CURSOR_CLI" >> "$TUNNEL_LOG"
        echo "Error: Cursor CLI not found. Please install it first." >&2
        return 1
    fi
    
    # Start tunnel in background
    (
        # Set up authentication if needed
        if ! "$CURSOR_CLI" tunnel user show &>/dev/null; then
            echo "[$(date)] Authenticating with GitHub..." >> "$TUNNEL_LOG"
            CURSOR_CLI_DISABLE_KEYCHAIN_ENCRYPT=1 \
                "$CURSOR_CLI" tunnel user login --provider github
        fi
        
        # Launch tunnel
        "$CURSOR_CLI" tunnel \
            --name "$TUNNEL_NAME" \
            --accept-server-license-terms 2>&1 | \
            while IFS= read -r line; do
                echo "[CURSOR] $line" >> "$TUNNEL_LOG"
                
                # Show tunnel URL to user
                if [[ "$line" == *"https://"* ]]; then
                    echo "🔗 Cursor tunnel available at: $(echo "$line" | grep -oE 'https://[^ ]+' | head -1)"
                fi
            done
    ) &
    
    CURSOR_TUNNEL_PID=$!
    echo "[$(date)] Cursor tunnel started with PID: $CURSOR_TUNNEL_PID" >> "$TUNNEL_LOG"
    
    # Give tunnel time to start
    sleep 3
    
    # Verify tunnel is running
    if kill -0 "$CURSOR_TUNNEL_PID" 2>/dev/null; then
        echo "✅ Cursor tunnel running (PID: $CURSOR_TUNNEL_PID)"
        return 0
    else
        echo "[$(date)] ERROR: Cursor tunnel failed to start" >> "$TUNNEL_LOG"
        echo "❌ Failed to start Cursor tunnel" >&2
        return 1
    fi
}

# Launch Claude with provided arguments
launch_claude() {
    local claude_args=("$@")
    
    echo "[$(date)] Launching Claude with args: ${claude_args[*]}" >> "$TUNNEL_LOG"
    
    # Launch Claude with all provided arguments
    "$CLAUDE_COMMAND" "${claude_args[@]}" &
    CLAUDE_PID=$!
    
    # Wait for Claude to finish
    wait "$CLAUDE_PID"
    local claude_exit_code=$?
    
    echo "[$(date)] Claude exited with code: $claude_exit_code" >> "$TUNNEL_LOG"
    return $claude_exit_code
}

# Cleanup function
cleanup() {
    echo "[$(date)] Cleaning up..." >> "$TUNNEL_LOG"
    
    # Terminate Cursor tunnel
    if [ -n "$CURSOR_TUNNEL_PID" ] && kill -0 "$CURSOR_TUNNEL_PID" 2>/dev/null; then
        echo "[$(date)] Terminating Cursor tunnel (PID: $CURSOR_TUNNEL_PID)" >> "$TUNNEL_LOG"
        kill -TERM "$CURSOR_TUNNEL_PID" 2>/dev/null || true
        
        # Wait for graceful shutdown
        local wait_count=0
        while kill -0 "$CURSOR_TUNNEL_PID" 2>/dev/null && [ $wait_count -lt 5 ]; do
            sleep 1
            ((wait_count++))
        done
        
        # Force kill if necessary
        if kill -0 "$CURSOR_TUNNEL_PID" 2>/dev/null; then
            echo "[$(date)] Force killing Cursor tunnel" >> "$TUNNEL_LOG"
            kill -KILL "$CURSOR_TUNNEL_PID" 2>/dev/null || true
        fi
    fi
    
    echo "[$(date)] Cleanup completed" >> "$TUNNEL_LOG"
    echo "🔚 Cursor tunnel closed"
}

# Main execution
main() {
    # Setup
    setup_logging
    generate_tunnel_name
    
    # Set up signal handlers
    trap cleanup EXIT SIGINT SIGTERM
    
    echo "🚀 Starting Cursor tunnel for project..."
    
    # Launch tunnel
    if launch_cursor_tunnel; then
        # Launch Claude with all arguments passed to this script
        launch_claude "$@"
    else
        echo "Failed to launch Cursor tunnel" >&2
        exit 1
    fi
}

# Execute main function with all script arguments
main "$@"