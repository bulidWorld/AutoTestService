#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

stop_service() {
    local pid deadline
    if ! pid="$(active_pid)"; then
        clear_pid
        echo "$SERVICE_NAME is not running"
        return 0
    fi
    kill -TERM "$pid" 2>/dev/null || true
    deadline=$((SECONDS + STOP_TIMEOUT))
    while active_pid >/dev/null && (( SECONDS < deadline )); do sleep 0.2; done
    if active_pid >/dev/null; then
        echo "Graceful shutdown timed out, killing pid=$pid" >&2
        kill -KILL "$pid" 2>/dev/null || true
        deadline=$((SECONDS + 5))
        while active_pid >/dev/null && (( SECONDS < deadline )); do sleep 0.2; done
        if active_pid >/dev/null; then
            echo '停止失败，保留 PID 供后续检查' >&2
            return 1
        fi
    fi
    clear_pid
    echo "$SERVICE_NAME stopped"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    [[ $# == 0 ]] || { echo '用法：stop.sh' >&2; exit 1; }
    lock_operations
    stop_service
fi
