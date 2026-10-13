#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
source "$SERVICE_ROOT/scripts/stop.sh"
source "$SERVICE_ROOT/scripts/start.sh"
[[ $# == 0 ]] || { echo '用法：restart.sh' >&2; exit 1; }
lock_operations
stop_service
start_service
