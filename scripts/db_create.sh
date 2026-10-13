#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
[[ $# == 0 ]] || { echo '用法：db_create.sh' >&2; exit 1; }
require_commands psql openssl stat
lock_operations

# Check authentication before generating an application password or changing the DB.
# This query contains no credentials, so its connection errors can be shown safely.
if ! psql_database ADMIN -Atc 'SELECT 1' >/dev/null; then
    printf '管理员连接失败：host=%s port=%s user=%s database=%s\n' \
        "$ADMIN_HOST" "$ADMIN_PORT" "$ADMIN_USER" "$ADMIN_NAME" >&2
    if [[ "$ADMIN_AUTH" == peer ]]; then
        printf 'peer 认证需以系统用户 %s 连接，请检查 sudo 权限及本机 pg_hba.conf\n' \
            "$ADMIN_SYSTEM_USER" >&2
    elif [[ ! -f "$ADMIN_PASSWORD_FILE" ]]; then
        printf '管理员密码文件不存在：%s，请写入该账号现有密码并设置权限为 600\n' \
            "$ADMIN_PASSWORD_FILE" >&2
    fi
    exit 1
fi

# The application password is generated only once and never printed.
if [[ ! -e "$DB_PASSWORD_FILE" ]]; then
    mkdir -p "$(dirname -- "$DB_PASSWORD_FILE")"
    (umask 077; set -o noclobber; openssl rand -hex 32 > "$DB_PASSWORD_FILE")
fi
APP_DB_PASSWORD="$(read_password "$DB_PASSWORD_FILE")"
[[ -n "$APP_DB_PASSWORD" ]] || { echo '应用密码文件无效' >&2; exit 1; }
export APP_DB_PASSWORD

# A single psql session holds the advisory lock through all provisioning SQL.
# \gexec executes generated CREATE statements outside a transaction block.
# \getenv avoids placing the application password in CLI arguments (psql 15+).
if ! psql_database ADMIN -v app_user="$DB_USER" -v app_database="$DB_NAME" >/dev/null 2>&1 <<'SQL'
\getenv app_password APP_DB_PASSWORD
SELECT pg_advisory_lock(739428150);
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')
\gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'app_database', :'app_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'app_database')
\gexec
-- Division by zero fails when an existing database has an unexpected owner.
SELECT 1 / count(*)::integer FROM pg_database
WHERE datname = :'app_database' AND pg_get_userbyid(datdba) = :'app_user';
SELECT pg_advisory_unlock(739428150);
SQL
then
    unset APP_DB_PASSWORD
    echo '数据库创建失败，请检查管理员认证、sudo 权限及已有数据库所有者（需 psql 15+）' >&2
    exit 1
fi
unset APP_DB_PASSWORD
check_database
echo 'Database and user ready'
