#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
[[ $# == 0 ]] || { echo '用法：status.sh' >&2; exit 1; }
lock_operations
pid='-'
status=STOPPED
if pid="$(active_pid)"; then status=RUNNING; else pid='-'; fi
database=UNAVAILABLE
if check_database 2>/dev/null; then database=OK; fi
printf 'Service: %s\nStatus: %s\nPID: %s\nPort: %s\nDatabase: %s\n' \
    "$SERVICE_NAME" "$status" "$pid" "$SERVICE_PORT" "$database"
[[ "$status" == RUNNING && "$database" == OK ]]
