#!/usr/bin/env bash
# Shared configuration, locking and process identity helpers. No lifecycle dispatch.
set -euo pipefail
if [[ ${SERVICE_COMMON_LOADED:-0} == 1 ]]; then
    return 0
fi
SERVICE_COMMON_LOADED=1
SERVICE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_PYTHON="$SERVICE_ROOT/.venv/bin/python"
if [[ ! -x "$SERVICE_PYTHON" ]]; then
    echo '缺少 .venv，请先按 README 安装依赖' >&2
    exit 1
fi
cd "$SERVICE_ROOT"

# Python only parses and validates the shared YAML; shlex.quote protects shell values.
if ! SERVICE_CONFIG_VALUES="$("$SERVICE_PYTHON" - <<'PY'
import shlex
import sys
from app.config.settings import absolute, load_settings
try:
    settings = load_settings()
    service, database = settings['service'], settings['database']
    values = {
        'SERVICE_NAME': service['name'], 'SERVICE_HOST': service['host'],
        'SERVICE_PORT': service['port'], 'STARTUP_TIMEOUT': service['startup_timeout'],
        'STOP_TIMEOUT': service['stop_timeout'],
        'LOG_DIRECTORY': absolute(settings['logging']['directory']),
        'SCHEMA_DIRECTORY': absolute(database['schema_directory']),
        'DB_CONNECT_TIMEOUT': database['connect_timeout'],
    }
    for prefix, source in [('DB', database), ('ADMIN', database['admin'])]:
        for key in ['host', 'port', 'name', 'user']:
            values[f'{prefix}_{key.upper()}'] = source[key]
        password_file = source.get('password_file')
        values[f'{prefix}_PASSWORD_FILE'] = absolute(password_file) if password_file else ''
    values['ADMIN_AUTH'] = database['admin'].get('auth', 'password')
    values['ADMIN_SYSTEM_USER'] = database['admin'].get('system_user', database['admin']['user'])
    print('\n'.join(f'{key}={shlex.quote(str(value))}' for key, value in values.items()))
except Exception as exc:
    print(f'配置读取失败，请检查 application.yaml 和覆盖配置：{exc}', file=sys.stderr)
    sys.exit(1)
PY
)"; then
    exit 1
fi
eval "$SERVICE_CONFIG_VALUES"
unset SERVICE_CONFIG_VALUES
PID_FILE="$SERVICE_ROOT/run/auto-test-service.pid"
IDENTITY_FILE="$PID_FILE.start"

require_commands() {
    local command
    for command in "$@"; do
        if ! command -v "$command" >/dev/null 2>&1; then
            echo "缺少命令：$command" >&2
            return 1
        fi
    done
}

lock_operations() {
    require_commands flock
    mkdir -p "$SERVICE_ROOT/run"
    exec 9>"$SERVICE_ROOT/run/control.lock"
    flock -x 9
}

process_identity() {
    local pid="$1" stat_line
    local -a fields
    [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 && -r "/proc/$pid/stat" ]] || return 1
    IFS= read -r stat_line < "/proc/$pid/stat" || return 1
    # Strip PID and comm (which may contain spaces); field 22 is starttime.
    read -r -a fields <<< "${stat_line##*) }"
    [[ ${fields[0]:-Z} != Z && ${fields[19]:-} =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "${fields[19]}"
}

active_pid() {
    local pid expected current
    [[ -r "$PID_FILE" && -r "$IDENTITY_FILE" ]] || return 1
    IFS= read -r pid < "$PID_FILE" || return 1
    IFS= read -r expected < "$IDENTITY_FILE" || return 1
    current="$(process_identity "$pid")" || return 1
    [[ "$current" == "$expected" ]] || return 1
    printf '%s\n' "$pid"
}

clear_pid() {
    rm -f -- "$PID_FILE" "$IDENTITY_FILE"
}

read_password() {
    local path="$1" mode value
    [[ -f "$path" ]] || return 0  # Allow PostgreSQL local/peer authentication.
    mode="$(stat -c '%a' -- "$path")"
    if (( (8#$mode & 077) != 0 )); then
        echo '数据库密码文件权限必须为 0600' >&2
        return 1
    fi
    value="$(< "$path")"
    [[ -n "$value" ]] || { echo '数据库密码文件不能为空' >&2; return 1; }
    printf '%s' "$value"
}

psql_database() {
    local prefix="$1" password
    local host_key="${prefix}_HOST" port_key="${prefix}_PORT"
    local name_key="${prefix}_NAME" user_key="${prefix}_USER" secret_key="${prefix}_PASSWORD_FILE"
    require_commands psql
    if [[ "$prefix" == ADMIN && "$ADMIN_AUTH" == peer ]]; then
        if [[ "$ADMIN_HOST" != /* ]]; then
            echo 'peer 管理员连接必须使用 Unix socket 目录，例如 /var/run/postgresql' >&2
            return 1
        fi
        local psql_binary
        psql_binary="$(command -v psql)"
        local -a peer_command=("$psql_binary" -X -w -q -v ON_ERROR_STOP=1
            -h "$ADMIN_HOST" -p "$ADMIN_PORT" -U "$ADMIN_USER" -d "$ADMIN_NAME")
        if [[ "$(id -un)" == "$ADMIN_SYSTEM_USER" ]]; then
            env -u PGPASSWORD PGCONNECT_TIMEOUT="$DB_CONNECT_TIMEOUT" \
                "${peer_command[@]}" "$@"
        else
            require_commands sudo
            # Run only psql as the matching OS user. The app password is passed
            # through an explicitly preserved environment variable, never argv.
            sudo --user="$ADMIN_SYSTEM_USER" --preserve-env=APP_DB_PASSWORD -- \
                env -u PGPASSWORD PGCONNECT_TIMEOUT="$DB_CONNECT_TIMEOUT" \
                "${peer_command[@]}" "$@"
        fi
        return
    fi
    if [[ "$prefix" == ADMIN && "$ADMIN_AUTH" != password ]]; then
        echo '不支持的管理员认证方式，仅支持 peer 或 password' >&2
        return 1
    fi
    password="$(read_password "${!secret_key}")" || return 1
    # Passwords enter libpq through environment, never command-line arguments.
    PGPASSWORD="$password" PGCONNECT_TIMEOUT="$DB_CONNECT_TIMEOUT" \
        psql -X -w -q -v ON_ERROR_STOP=1 -h "${!host_key}" -p "${!port_key}" \
        -U "${!user_key}" -d "${!name_key}" "$@"
}

check_database() {
    if ! psql_database DB -Atc 'SELECT 1' >/dev/null 2>&1; then
        echo '应用数据库连接失败，请检查数据库配置和密码文件' >&2
        return 1
    fi
}
