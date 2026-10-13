# AutoTestService 初始化与运行步骤

以下以本项目路径 `/opt/apps/Deployment/AutoTestService` 为例，使用 Bash 按第 1～9 步顺序执行。应用数据库密码在命令提示时输入；管理员使用本机 peer，不设置数据库密码。

本次示例使用以下参数；如果你的数据库端口、socket 或应用账号不同，修改第 3 步的 YAML，以及第 4、6、7 步的 psql 参数：

| 参数 | 本文使用的值 |
| --- | --- |
| 项目目录 | `/opt/apps/Deployment/AutoTestService` |
| PostgreSQL 地址与端口 | `127.0.0.1:5432` |
| 管理员账号 / 已有数据库 | `postgres` / `postgres` |
| 管理员认证 / socket / 系统账号 | `peer` / `/var/run/postgresql` / `postgres` |
| 应用账号 / 要创建的数据库 | `auto_test_service` / `auto_test_service` |
| 应用监听地址与端口 | `127.0.0.1:31010` |
| 覆盖配置 | `config/application-local.yaml` |
| 日志目录 | 项目下的 `logs/` |

`db_create.sh`、`start.sh` 等脚本不接受 `--host`、`--password` 参数，连接参数从 YAML 读取。下面每次调用都用 `APP_CONFIG=...` 明确指定配置，无需事先 export。

## 1. 进入项目并检查环境

需要 Python 3.11+、已启动的 PostgreSQL 服务、psql 15+。管理员账号需具备创建角色和数据库的权限。项目脚本不会安装或启动 PostgreSQL。

```bash
cd /opt/apps/Deployment/AutoTestService
python3 --version
psql --version
for cmd in curl flock ss openssl stat nohup; do command -v "$cmd"; done
```

若缺少命令，先安装对应的系统软件包，再继续。后续命令均在这个项目目录执行。

## 2. 创建虚拟环境并安装项目依赖

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --requirement requirements.lock
./.venv/bin/python -m pip install --no-deps --editable .
```

不需要执行 `source .venv/bin/activate`，项目脚本会直接使用 `.venv/bin/python`。

## 3. 写入本次部署的配置

以下命令写入本文的本地配置；若已有同名配置，先保留备份：

```bash
if [[ -f config/application-local.yaml ]]; then
    cp -p config/application-local.yaml "config/application-local.yaml.backup-$(date +%Y%m%d%H%M%S)"
fi

cat > config/application-local.yaml <<'YAML'
service:
  host: 127.0.0.1
  port: 31010
  startup_timeout: 20
  stop_timeout: 30
logging:
  directory: logs
database:
  host: 127.0.0.1
  port: 5432
  name: auto_test_service
  user: auto_test_service
  password_file: config/database.secret
  connect_timeout: 5
  schema_directory: db
  admin:
    auth: peer
    host: /var/run/postgresql
    port: 5432
    name: postgres
    user: postgres
    system_user: postgres
YAML
```

未指定的项继承 `config/application.yaml`。`admin.name` 是用于连接的已有数据库；`database.name` 是下一步要创建的应用数据库。应用密码保存在 `config/database.secret`；管理员通过 socket 使用 peer，无需密码文件。

## 4. 验证管理员 peer 连接

```bash
sudo -u postgres psql --no-psqlrc \
    --host=/var/run/postgresql \
    --port=5432 \
    --username=postgres \
    --dbname=postgres \
    --command='SELECT current_user, current_database();'
```

如 sudo 要求密码，输入当前 Linux 账号的 sudo 密码。无需 PostgreSQL 管理员密码，不需要创建 `config/database-admin.secret`。

应返回用户 `postgres`、数据库 `postgres`。管理员脚本使用相同的 socket 和系统身份，只提升 psql 的权限；应用、密码文件和项目运行目录仍由当前部署账号管理。若已有旧的 local 配置，确认第 3 步已将管理员改为 `auth: peer`，并将 host 设为 socket 目录。

## 5. 指定应用数据库密码

首次创建应用账号时，输入你希望设置的新密码。已有 `auto_test_service` 账号时，输入该账号的现有密码。

```bash
read -r -s -p '请输入 auto_test_service 应用数据库密码: ' SERVICE_DB_PASSWORD
printf '\n'
if [[ -n "$SERVICE_DB_PASSWORD" ]]; then
    (umask 077; printf '%s\n' "$SERVICE_DB_PASSWORD" > config/database.secret)
    chmod 600 config/database.secret
else
    printf '密码不能为空，请重新执行本步骤\n' >&2
fi
unset SERVICE_DB_PASSWORD

stat --format='%a %U %n' config/database.secret
```

应看到应用密码文件权限为 `600`。密码使用非空单行文本，不加多余空白；输入不会显示在终端，也不会作为命令参数保存。

若希望自动生成应用密码，可跳过本步骤的输入命令。第 6 步只在 `config/database.secret` 不存在时生成随机密码；已有文件保持不变。

## 6. 创建数据库与应用账号

```bash
APP_CONFIG=config/application-local.yaml ./scripts/db_create.sh
```

此命令通过本机 peer 管理员连接创建 `auto_test_service` 登录角色和同名数据库，并设置应用角色为数据库所有者。成功输出：

```text
Database and user ready
```

再用应用账号验证连接，按提示输入第 5 步设置的应用密码：

```bash
psql --no-psqlrc \
    --host=127.0.0.1 \
    --port=5432 \
    --username=auto_test_service \
    --dbname=auto_test_service \
    --password \
    --command='SELECT current_user, current_database();'
```

应返回用户与数据库均为 `auto_test_service`。选择自动生成密码时，可跳过该交互检查，创建脚本已经用密码文件完成了连接验证。

创建命令可重复执行，不删除数据，不修改已有角色密码。仅修改 `.secret` 文件不能重置已有角色密码；已有数据库必须由应用角色拥有。

## 7. 初始化表结构

```bash
APP_CONFIG=config/application-local.yaml ./scripts/db_init.sh
```

应看到 `Schema synchronization completed.`。查看初始化结果：

```bash
psql --no-psqlrc \
    --host=127.0.0.1 \
    --port=5432 \
    --username=auto_test_service \
    --dbname=auto_test_service \
    --password \
    --command='\dt public.*'
```

应看到 `projects`、`yaml_files`、`yaml_versions`、`api_interfaces` 四张阶段一业务表，以及 `schema_xml_migration` 和 `database_change_log` 两张迁移元数据表。任务表留待后续阶段。自动生成密码时可跳过这个交互查询。

## 8. 启动并检查状态

```bash
APP_CONFIG=config/application-local.yaml ./scripts/start.sh
APP_CONFIG=config/application-local.yaml ./scripts/status.sh
```

预期状态输出中的关键字段：

```text
Service: auto-test-service
Status: RUNNING
PID: 实际进程号
Port: 31010
Database: OK
```

启动脚本会检查数据库、同步 XML、检查端口，并等待接口就绪。重复执行 `start.sh` 会提示已经运行，不会启动第二个实例。

## 9. 验证 HTTP 接口与访问页面

```bash
curl --noproxy '*' --fail --silent --show-error \
    --connect-timeout 5 --max-time 10 \
    http://127.0.0.1:31010/api/v1/health

curl --noproxy '*' --fail --silent --show-error \
    --connect-timeout 5 --max-time 10 \
    http://127.0.0.1:31010/api/v1/ready

curl --noproxy '*' --fail --silent --show-error \
    --connect-timeout 5 --max-time 10 \
    --output /tmp/auto-test-service-openapi.json \
    http://127.0.0.1:31010/openapi.json
```

两个健康接口均应返回 `{"status":"ok"}`。最后一条命令将接口契约保存到 `/tmp/auto-test-service-openapi.json`。

在服务器本机浏览器打开 `http://127.0.0.1:31010/docs` 查看接口文档。若浏览器运行在另一台电脑，可以在该电脑另开终端，替换 SSH 账号与服务器地址后建立隧道：

```bash
ssh -N -L 18080:127.0.0.1:31010 your_user@your_server
```

保持 SSH 连接，在电脑浏览器打开 `http://127.0.0.1:18080/docs`。这一步不需要修改服务监听地址。

## 10. 后续常用操作

完成初始化后，新终端只需进入项目目录，选择同一个配置进行操作，不用重新创建密码文件：

```bash
cd /opt/apps/Deployment/AutoTestService

# 查看状态
APP_CONFIG=config/application-local.yaml ./scripts/status.sh

# 重启
APP_CONFIG=config/application-local.yaml ./scripts/restart.sh

# 根据更新后的 XML 升级数据库
APP_CONFIG=config/application-local.yaml ./scripts/db_migrate.sh

# 仅预览同步 SQL，不修改数据库
APP_CONFIG=config/application-local.yaml ./scripts/db_sync.sh --dry-run

# 查看最近的错误与应用日志
tail -n 100 logs/error.log
tail -n 100 logs/application.log

# 停止服务
APP_CONFIG=config/application-local.yaml ./scripts/stop.sh
```

`status.sh` 在服务未运行或数据库不可用时返回非 0。数据库升级时请按实际变更安排停止和启动服务，避免运行中的代码与表结构不兼容。

更多生产配置、密码变更和故障处理见 [README.md](README.md)。
