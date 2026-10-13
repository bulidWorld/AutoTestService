#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

sync_database() {
    check_database
    "$SERVICE_PYTHON" "$SERVICE_ROOT/scripts/sync_schema.py" --dir "$SCHEMA_DIRECTORY" "$@"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    lock_operations
    sync_database "$@"
fi
