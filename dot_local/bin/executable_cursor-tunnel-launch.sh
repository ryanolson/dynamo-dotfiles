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

# Generate tunnel name from machine identity
generate_tunnel_name() {
    local tunnel_name=""
    
    # Get hostname
    local hostname=$(hostname)
    
    # Get public IP and convert dots to hyphens
    local public_ip=$(curl -s --max-time 2 ifconfig.me 2>/dev/null || echo "")
    local ip_dashed="${public_ip//./-}"
    
    # Determine tunnel name based on hostname and IP
    if [ -z "$public_ip" ]; then
        # Couldn't get public IP, just use hostname
        tunnel_name="$hostname"
    elif [ "$hostname" = "$ip_dashed" ]; then
        # Hostname is already the IP (with dashes), use it once
        tunnel_name="$hostname"
    else
        # Hostname differs from IP, combine them
        tunnel_name="${hostname}-${ip_dashed}"
    fi
    
    # Clean tunnel name (ensure only alphanumeric and hyphens)
    tunnel_name=${tunnel_name//[^a-zA-Z0-9-]/-}
    
    # Ensure tunnel name isn't too long (cursor has a 50 char limit)
    if [ ${#tunnel_name} -gt 50 ]; then
        # Truncate and add hash for uniqueness
        local hash=$(echo "$hostname-$public_ip" | sha256sum | cut -c1-6)
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
    
    # Launch tunnel in background, redirect output to file for processing
    local output_file="$TUNNEL_DIR/tunnel_output.$$"
    
    "$CURSOR_CLI" tunnel \
        --name "$tunnel_name" \
        --accept-server-license-terms > "$output_file" 2>&1 &
    
    # Immediately save the tunnel PID (this should be the actual cursor process)
    local tunnel_pid=$!
    echo "$tunnel_pid" > "$MASTER_LOCK.real"
    echo "[$(date)] Started cursor tunnel with PID: $tunnel_pid" >> "$TUNNEL_LOG"
    
    # Process output in background
    (
        tail -f "$output_file" 2>/dev/null | while IFS= read -r line; do
            echo "[CURSOR] $line" >> "$TUNNEL_LOG"
            
            # Show tunnel URL only once when it starts
            if [[ "$line" == *"https://"* ]] && [ ! -f "$LOCK_DIR/url_shown" ]; then
                echo "🔗 Cursor tunnel available at: $(echo "$line" | grep -oE 'https://[^ ]+' | head -1)"
                touch "$LOCK_DIR/url_shown"
            fi
        done
    ) &
    
    # Save the tail PID so we can clean it up later
    local tail_pid=$!
    echo "$tail_pid" > "$MASTER_LOCK.tail"
    
    # Clean up output file on exit
    trap "rm -f $output_file" EXIT
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
        
        # Start tunnel (it handles backgrounding internally)
        start_tunnel_process "$tunnel_name"
        
        # Read the saved PID
        local tunnel_pid=$(cat "$MASTER_LOCK.real" 2>/dev/null || echo "")
        
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
                    
                    # Kill the actual cursor tunnel process
                    if [ -f "$MASTER_LOCK.real" ]; then
                        local real_pid=$(cat "$MASTER_LOCK.real")
                        echo "[$(date)] Stopping cursor tunnel PID: $real_pid" >> "$TUNNEL_LOG"
                        kill -TERM "$real_pid" 2>/dev/null || true
                    fi
                    
                    # Kill the tail process if it exists
                    if [ -f "$MASTER_LOCK.tail" ]; then
                        local tail_pid=$(cat "$MASTER_LOCK.tail")
                        echo "[$(date)] Stopping tail process PID: $tail_pid" >> "$TUNNEL_LOG"
                        kill -TERM "$tail_pid" 2>/dev/null || true
                    fi
                    
                    # Kill by name as fallback
                    if [ -f "$NAME_FILE" ]; then
                        local tunnel_name=$(cat "$NAME_FILE")
                        pkill -f "cursor tunnel --name $tunnel_name" 2>/dev/null || true
                    fi
                    
                    # Give it time to cleanup
                    local wait_count=0
                    while (kill -0 "$tunnel_pid" 2>/dev/null || ([ -f "$MASTER_LOCK.real" ] && kill -0 "$(cat "$MASTER_LOCK.real")" 2>/dev/null)) && [ $wait_count -lt 5 ]; do
                        sleep 1
                        ((wait_count++))
                    done
                    
                    # Force kill if necessary
                    if kill -0 "$tunnel_pid" 2>/dev/null; then
                        kill -KILL "$tunnel_pid" 2>/dev/null || true
                    fi
                    if [ -f "$MASTER_LOCK.real" ]; then
                        local real_pid=$(cat "$MASTER_LOCK.real")
                        if kill -0 "$real_pid" 2>/dev/null; then
                            kill -KILL "$real_pid" 2>/dev/null || true
                        fi
                    fi
                    
                    # Clean up all files
                    rm -f "$MASTER_LOCK" "$MASTER_LOCK.real" "$MASTER_LOCK.tail" "$NAME_FILE" "$LOCK_DIR/url_shown"
                    rm -f "$TUNNEL_DIR"/tunnel_output.*
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

# Generate and display clickable tunnel URLs
display_tunnel_urls() {
    local tunnel_name="$1"
    local working_dir="${2:-$(pwd)}"
    
    # Base tunnel URL
    local base_url="https://vscode.dev/tunnel/${tunnel_name}"
    
    echo ""
    echo "🔗 Tunnel URLs:"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    # Web browser URL (always works)
    echo "🌐 Browser: $base_url/"
    
    # Generate clickable Cursor URL for folder using OSC 8 (with new window parameter)
    local cursor_folder_url="cursor://vscode-remote/tunnel+${tunnel_name}${working_dir}?windowId=_blank"
    printf '📂 Open folder: \033]8;;%s\033\\Click to open in Cursor (new window)\033]8;;\033\\\n' "$cursor_folder_url"
    
    # Check for workspace file
    local workspace_files=(*.code-workspace)
    if [ ${#workspace_files[@]} -eq 1 ] && [ -f "${workspace_files[0]}" ]; then
        local workspace_path="${working_dir}/${workspace_files[0]}"
        local cursor_workspace_url="cursor://vscode-remote/tunnel+${tunnel_name}${workspace_path}?windowId=_blank"
        printf '📄 Open workspace: \033]8;;%s\033\\%s (Click to open in Cursor - new window)\033]8;;\033\\\n' "$cursor_workspace_url" "${workspace_files[0]}"
    fi
    
    # Check if running in Zellij and show tip
    if [ -n "${ZELLIJ:-}" ]; then
        echo "💡 Zellij tip: Hold SHIFT while clicking links to open them"
    fi
    
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
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
    
    # Display tunnel URLs if tunnel is running
    if [ -f "$NAME_FILE" ]; then
        local tunnel_name=$(cat "$NAME_FILE")
        display_tunnel_urls "$tunnel_name" "$(pwd)"
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