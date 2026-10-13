#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
source "$SERVICE_ROOT/scripts/db_sync.sh"

start_service() {
    local pid identity host deadline
    if pid="$(active_pid)"; then
        echo "$SERVICE_NAME is already running, pid=$pid"
        return 0
    fi
    clear_pid
    require_commands curl ss nohup
    mkdir -p "$SERVICE_ROOT/run" "$LOG_DIRECTORY"
    sync_database
    # Prevent another HTTP listener from being mistaken for our readiness endpoint.
    if [[ -n "$(ss -H -ltn "sport = :$SERVICE_PORT")" ]]; then
        echo "启动失败：端口 $SERVICE_PORT 已被占用" >&2
        return 1
    fi
    # Close the deployment-lock FD in the child; otherwise it would hold the lock.
    nohup "$SERVICE_PYTHON" -m app.main </dev/null >/dev/null 2>&1 9>&- &
    pid=$!
    if ! identity="$(process_identity "$pid")"; then
        wait "$pid" 2>/dev/null || true
        echo '应用启动失败，请检查 application.log / error.log' >&2
        return 1
    fi
    printf '%s\n' "$identity" > "$IDENTITY_FILE"
    printf '%s\n' "$pid" > "$PID_FILE"
    host="$SERVICE_HOST"
    case "$host" in
        0.0.0.0) host=127.0.0.1 ;;
        ::) host='[::1]' ;;
        *:*) host="[$host]" ;;
    esac
    deadline=$((SECONDS + STARTUP_TIMEOUT))
    while (( SECONDS < deadline )); do
        active_pid >/dev/null || break
        if curl --noproxy '*' --silent --fail --max-time 1 \
            "http://$host:$SERVICE_PORT/api/v1/ready" >/dev/null 2>&1; then
            sleep 0.2
            if active_pid >/dev/null; then
                echo "$SERVICE_NAME started, pid=$pid"
                return 0
            fi
        fi
        sleep 0.2
    done
    if active_pid >/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
        deadline=$((SECONDS + 5))
        while active_pid >/dev/null && (( SECONDS < deadline )); do sleep 0.2; done
        if active_pid >/dev/null; then kill -KILL "$pid" 2>/dev/null || true; fi
    fi
    wait "$pid" 2>/dev/null || true
    clear_pid
    echo '应用启动失败，请检查 application.log / error.log' >&2
    return 1
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    [[ $# == 0 ]] || { echo '用法：start.sh' >&2; exit 1; }
    lock_operations
    start_service
fi
