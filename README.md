# AutoTestService

接口自动化测试服务的 Python 单体应用。阶段一已按 [OpenAPI YAML 契约](docs/rest-project.yaml) 实现项目管理、YAML 导入与全量更新、历史版本、接口浏览及关联删除，使用 PostgreSQL 持久化。浏览器访问 `/` 使用管理页面，访问 `/docs` 查看接口文档。业务需求见 [需求总览](context/requirement/overview.md)。

首次部署可直接按 [逐步初始化与运行命令](step_by_step.md) 执行，包含明确的数据库连接参数和密码配置步骤。

## 目录与职责

```text
app/
  main.py          应用组装与启动
  api/             YAML 动态注册、显式 Handler 映射
  service/         业务服务
  repository/      数据库访问
  model/           领域模型（后续业务实现）
  config/          配置加载与 JSON 日志
config/            基础、生产及本地配置
scripts/           标准启停和数据库脚本
db/                XML 表定义与 database.xsd
docs/rest-project.yaml  HTTP 接口唯一契约
tests/             基础行为验证
logs/              预留目录；默认实际日志在 /var/service/log/auto-test-service/
run/               PID 和部署操作锁
```

## 首次部署：安装依赖

要求 Linux、Python 3.11+、已运行的 PostgreSQL，以及 psql 15+、curl、flock、ss、openssl、stat。`requirements.lock` 固定基础和开发依赖版本，`requirements.txt` 声明运行依赖范围。

以下命令使用 Bash。先进入项目目录；如果部署到其他位置，替换第一行路径：

```bash
cd /opt/apps/Deployment/AutoTestService
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
```

可以检查所需系统命令是否已安装：

```bash
python3 --version
psql --version
for cmd in curl flock ss openssl stat; do command -v "$cmd"; done
```

脚本不会安装或启动 PostgreSQL 服务。执行下一步前，需确认数据库地址可以连接，并有一个具备创建角色、数据库权限的管理员账号。

## 配置数据库连接

配置分为两套连接：

| 配置位置 | 用途 | 默认值 |
| --- | --- | --- |
| `database.host / port` | 应用数据库服务器地址 | `127.0.0.1:5432` |
| `database.name` | 要创建或使用的数据库名称 | `auto_test_service` |
| `database.user` | 应用数据库账号，也是新数据库所有者 | `auto_test_service` |
| `database.password_file` | 应用账号密码文件 | `config/database.secret` |
| `database.admin.auth` | 管理员认证方式 | `peer` |
| `database.admin.host / port` | 管理员 Unix socket 连接 | `/var/run/postgresql`、`5432` |
| `database.admin.user / name` | 管理员角色及已有数据库 | `postgres` / `postgres` |
| `database.admin.system_user` | peer 认证使用的系统账号 | `postgres` |

`database.admin` 必须连接一个**已经存在**的数据库，例如 `postgres`。普通应用连接的目标数据库可以尚未创建。首次创建时，两套连接指向同一 PostgreSQL 实例；管理员走本机 socket，应用走 TCP。

推荐使用本地覆盖配置。下面是新建文件的示例；如果文件已经存在，编辑其中对应字段，保留已有配置：

```bash
cat > config/application-local.yaml <<'YAML'
service:
  host: 127.0.0.1
  port: 31010
logging:
  directory: logs
database:
  host: 127.0.0.1
  port: 5432
  name: auto_test_service
  user: auto_test_service
  password_file: config/database.secret
  admin:
    auth: peer
    host: /var/run/postgresql
    port: 5432
    name: postgres
    user: postgres
    system_user: postgres
YAML
export APP_CONFIG=config/application-local.yaml
```

替换上面的地址、端口和账号即可连接其他 PostgreSQL 实例。未列出的配置递归继承 `config/application.yaml`。配置中的文件路径相对于项目根目录解析，也支持绝对路径。

`application-local.yaml` **不会自动加载**。每次打开新的终端，执行操作前都需重新设置 `APP_CONFIG`。不设置时只读取 `application.yaml`；也可以直接修改基础配置，从而省去环境变量。一次只能指定一个覆盖文件，不能同时加载 local 和 prod。

## 指定数据库密码

### 1. 管理员使用本机 peer 认证

管理员不需要设置数据库密码，也不需要 `config/database-admin.secret`。先检查本机 socket 连接：

```bash
sudo -u postgres psql --no-psqlrc \
    --host=/var/run/postgresql --port=5432 \
    --username=postgres --dbname=postgres \
    --command='SELECT current_user, current_database();'
```

如 sudo 提示密码，输入当前 Linux 账号的 sudo 密码，不是数据库密码。peer 认证要求系统身份与数据库角色匹配；脚本仅将管理员 psql 命令切换为 `postgres` 系统用户，不将整个应用以 postgres 身份运行。当前系统用户已经是配置的 `system_user` 时直接执行 psql。

`pg_hba.conf` 需要允许对应 socket 连接使用 peer，本机默认管理员规则通常为 `local all postgres peer`。配置正确且具备 sudo 权限后，直接执行 `bash scripts/db_create.sh` 即可使用默认管理员连接；脚本忽略管理员密码文件，即使该文件为空或权限不正确，也不影响 peer 连接。

若显式选择远程管理员密码连接，应在覆盖配置中设置 `database.admin.auth: password`、TCP host 和 `password_file`；这是独立配置方式，本机初始化流程使用 peer。

### 2. 指定应用账号密码

**要使用自己指定的密码，必须在首次执行 `db_create.sh` 前写入应用密码文件。**

```bash
read -r -s -p '请输入要设置的应用数据库密码: ' SERVICE_DB_PASSWORD
printf '\n'
(umask 077; printf '%s\n' "$SERVICE_DB_PASSWORD" > config/database.secret)
chmod 600 config/database.secret
unset SERVICE_DB_PASSWORD
```

若修改了 `database.password_file`，同步替换上述文件路径。建议使用非空、单行密码，不在内容前后添加多余空白。密码文件仅保存密码文本，不写 `password=` 或 YAML 字段。

如果不指定密码，直接执行 `db_create.sh` 即可：脚本在文件不存在时用 `openssl` 随机生成密码，并以 `0600` 权限保存。已有密码文件不会被覆盖，脚本也不会打印密码。

应用 `.secret` 文件默认被 Git 忽略，应由部署账号持有，其他用户不可读写。可以只检查权限，不输出密码内容：

```bash
stat -c '%a %U %n' config/database.secret
```

应用密码文件权限应为 `600`。peer 管理员无需密码文件。

## 创建数据库与用户

确认覆盖配置已选择、本机 peer 管理员连接可用、应用密码已指定或选择自动生成后，执行：

```bash
./scripts/db_create.sh
```

默认配置下，脚本会：

1. 读取或首次生成 `config/database.secret`。
2. 以 `postgres` 系统身份通过 Unix socket 和 peer 认证连接已有的 `postgres` 数据库。
3. 查询应用角色是否存在；不存在时创建 `auto_test_service` 登录角色并设置应用密码。
4. 查询数据库是否存在；不存在时创建 `auto_test_service` 数据库，以应用角色为所有者。
5. 检查数据库所有者，再使用应用账号连接新数据库并执行 `SELECT 1`。

实际创建命令位于 `scripts/db_create.sh`，通过 psql 执行，逻辑等价于：

```sql
CREATE ROLE auto_test_service LOGIN PASSWORD '<应用密码文件中的值>';
CREATE DATABASE auto_test_service OWNER auto_test_service;
```

以上 SQL 用于说明，不需要手工执行，也不要将真实密码填写到命令历史中。脚本使用 PostgreSQL 的标识符和值转义，并通过同一会话的 advisory lock 防止并发重复创建。

成功时输出：

```text
Database and user ready
```

可重复运行验证幂等性：

```bash
./scripts/db_create.sh
```

重复执行不重建数据库、不删除数据，也不修改已有角色的密码。创建用户和数据库不是一个事务：中途失败可能已经创建用户，修正配置后可再次执行。

## 初始化表结构与启动

数据库创建完成后，先初始化表结构，再启动应用：

```bash
./scripts/db_init.sh
./scripts/start.sh
./scripts/status.sh
```

`db_init.sh` 使用**应用账号**校验并同步 XML；只有 `db_create.sh` 使用管理员账号。阶段一会建立 projects、yaml_files、yaml_versions、api_interfaces 四张业务表，以及迁移账本和变更审计表；任务表留待后续阶段。`start.sh` 也会先同步数据库，因此正常部署后可直接启动完成增量同步。

使用上述本地配置时，日志写入项目 `logs/`。成功启动后，`status.sh` 应显示 `RUNNING` 和 `Database: OK`，可以继续检查：

```bash
curl --fail http://127.0.0.1:31010/api/v1/health
curl --fail http://127.0.0.1:31010/api/v1/ready
```

两者正常返回 `{"status":"ok"}`。如果配置了其他监听地址或端口，请替换 URL。

浏览器入口：

- `http://127.0.0.1:31010/docs`：与 YAML 一致的 Swagger 文档。
- `http://127.0.0.1:31010/openapi.json`：原始契约。

默认只允许从服务器本机访问。如需远程访问，在配置中将 `service.host` 设为 `0.0.0.0`，浏览器使用实际服务器地址，网络需允许该端口。

## 日常启停与生产配置

| 命令 | 作用 |
| --- | --- |
| `./scripts/start.sh` | 检查配置、数据库、端口，同步结构，启动并等待就绪 |
| `./scripts/status.sh` | 查看进程、端口和数据库；未运行或数据库不可用时退出码为非 0 |
| `./scripts/restart.sh` | 在同一操作锁下先停止再启动 |
| `./scripts/stop.sh` | 先发送 SIGTERM，超时后发送 SIGKILL |
| `./scripts/db_sync.sh` | 同步 XML 定义的表结构和初始化数据 |
| `./scripts/db_migrate.sh` | 与 db_sync 使用同一入口，适用于升级操作 |

重复启停幂等，已启动时不会创建第二个实例。PID 启动标识用于防止过期 PID 误杀其他进程；`.pid` 保存数字 PID，`.pid.start` 保存启动标识，不应手工修改。启动失败返回非 0，并清理本次运行的 PID 文件。

脚本使用绝对部署路径，不依赖调用时的工作目录。在其他目录执行时，使用脚本绝对路径；`APP_CONFIG` 的相对路径仍相对于项目根目录。

生产部署编辑 `config/application-prod.yaml`，加入实际数据库及密码文件配置，选择生产覆盖文件：

```bash
export APP_CONFIG=config/application-prod.yaml
```

该操作会替换之前选择的 local 覆盖配置，不会继承其中的数据库或日志设置。生产默认日志目录为 `/var/service/log/auto-test-service/`。由有权限的部署人员为服务账号创建目录；下面示例以当前账号作为服务账号：

```bash
sudo install -d -m 0750 -o "$(id -un)" -g "$(id -gn)" /var/service/log/auto-test-service
./scripts/start.sh
./scripts/status.sh
```

## 已有数据库与密码变更

已有数据库需由配置中的应用角色拥有，应用密码文件需保存该角色的实际密码。角色已存在时，`db_create.sh` **不会通过文件内容重设数据库密码**；仅修改文件会导致密码认证连接失败。

如果数据库和账号已由 DBA 创建且所有者一致，可直接配置现有密码，再执行：

```bash
./scripts/db_init.sh
./scripts/start.sh
```

需要变更默认应用角色密码时，可使用管理员交互式操作；按实际配置替换管理员连接参数及角色名：

```bash
./scripts/stop.sh
sudo -u postgres psql -X -h /var/run/postgresql -p 5432 -U postgres -d postgres
```

进入 psql 后执行，按提示输入新的应用密码：

```text
\password auto_test_service
\q
```

然后按照“指定应用账号密码”的输入命令，将同一个新密码写入 `config/database.secret`，再执行 `./scripts/start.sh`。不要删除已有密码文件期待脚本自动重置角色密码；生成的新密码不会自动应用到已有角色。

## 常见操作错误

| 现象 | 检查和处理 |
| --- | --- |
| 缺少 `.venv` | 在项目目录完成依赖安装 |
| 配置读取失败 | 检查 YAML 格式、契约文件路径，以及 `APP_CONFIG` 是否指向正确文件 |
| 密码文件权限错误 | 对配置中的密码文件执行 `chmod 600`，确认部署账号拥有读取权限 |
| 数据库创建失败 | 检查 socket 地址、sudo 权限、peer 规则、CREATEROLE/CREATEDB 权限、psql 版本及已有数据库所有者 |
| `Peer authentication failed` | 确认管理员 host 为 socket 目录，系统账号与数据库角色匹配，且本机认证规则为 peer |
| `fe_sendauth: no password supplied` | 检查是否加载了旧的 TCP 管理员配置；本机管理员应设置 `auth: peer` 和 socket host。应用连接则检查 `database.secret` |
| 应用数据库连接失败 | 检查应用数据库地址、数据库是否存在、账号密码，以及 PostgreSQL 的认证配置 |
| 端口已被占用 | 用 `ss -ltn` 检查监听端口，停止冲突服务或修改 `service.port` |
| 日志目录无权限 | 创建日志目录并赋予服务账号权限，或开发时将日志目录设为 `logs` |
| `status.sh` 返回非 0 | 根据输出区分服务未启动和数据库不可用，再查看日志 |

本地日志查看命令：

```bash
tail -n 100 logs/error.log
tail -n 100 logs/application.log
```

生产部署请替换为实际 `logging.directory` 下的文件。脚本错误输出会隐藏数据库凭据；需要进一步诊断连接时，管理员使用上文的 sudo/socket 命令，应用使用带 `-W` 的 psql 命令交互输入应用密码。

## 接口开发规则

每个接口 YAML 对应一个 Python 实现文件：`docs/rest-project.yaml` 对应
`app/repository/rest_project.py`，在 `app/api/registry.py` 的
`CONTRACT_IMPLEMENTATIONS` 中按 YAML 文件名（不含扩展名）注册。
每个 `operationId` 对应实现文件中一个同名函数，例如 `createProject` 对应
`def createProject(...)`，函数文档字符串注明 HTTP 方法和路径；`OPERATIONS`
显式映射到函数，路由注册时直接绑定，无需经过 operation 字符串分支。
`app/api/business.py` 保留公共请求校验；`app/repository/projects.py` 仅保留数据库、
分页、导入保存、关联清理和事务公共方法；导入解析与脱敏在
`app/service/openapi_import.py`。

新增接口时，在对应 YAML 中定义 operationId，再在对应 Python 文件添加同名函数
并注册到 `OPERATIONS`。新增 YAML 时，创建名称将 `-` 替换为 `_` 的对应 Python
文件并注册模块。未注册实现的操作返回 501。查找业务代码可以直接搜索：

```bash
rg -n '^def createProject\(' app/repository/rest_project.py
rg -n '^def ' app/repository/rest_project.py
```

分层方向为 API → service → repository/model。不要在 `main.py` 重写 YAML Schema。

## 数据库变更规则

XML 放在 `db/`，格式遵循 `db/database.xsd`；`db_sync.sh`、`db_init.sh`、`db_migrate.sh` 使用同一同步入口。`scripts/sync_schema.py` 复用开发规范的同步算法，连接改为统一配置。

```bash
./scripts/db_migrate.sh
```

启停、状态和数据库创建直接由 Shell 实现，`db_create.sh` 使用 psql 执行 CREATE ROLE / CREATE DATABASE；YAML 读取复用 Python 配置模块，表结构同步仍由规范提供的 `sync_schema.py` 执行。

同步前执行 XSD 校验。同步器保存校验和及变更审计，重复执行不重复应用未变化 SQL；自动增加缺失列，保留已有列。修改现有类型、约束、索引或初始化数据，应在 XML 的 `statements` 中添加有唯一 ID 的幂等 SQL，不能仅修改 CREATE TABLE 期待修改已有表。阶段一业务表与事务约束见 [数据库结构说明](db/README.md)。

## 日志与验证

application.log、error.log、access.log 使用 JSON，包含时间、级别、服务、进程、线程、模块、请求 ID。按大小轮转，默认 100 MiB、保留 30 个历史文件。HTTP 响应返回 `X-Request-ID`。不记录请求头、体、查询参数或原始异常文本，异常记录类型；后续业务日志同样必须遵守敏感信息规则。

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check app tests
for script in scripts/*.sh; do bash -n "$script"; done
```


阶段一验收：启动后访问 `http://127.0.0.1:31010/`。在项目内上传
`context/requirement/1-1.support_mang_proj/examples/valid.yaml`，然后用 `updated.yaml`
更新，验证新增、修改、移除接口及历史版本保留。`invalid.yaml` 用于验证错误定位与失败回滚。
文件限制为 UTF-8、OpenAPI 3.0.x、5 MiB；不支持的结构返回错误及 JSON Pointer。
查看版本内容会重新输出脱敏后的 YAML，因此不保留原始排版及注释。
删除操作先查询影响范围并二次确认；阶段一任务和运行计数为 0。

```bash
.venv/bin/pytest -q
AUTOTEST_DB_TESTS=1 .venv/bin/pytest -q
.venv/bin/ruff check app tests
```

数据库业务测试使用独立临时 schema，结束后清理；既有数据库约束测试回滚测试事务。


导入器支持 `application/json` 与 `multipart/form-data` 请求体定义，每个请求体限一种
媒体类型。multipart 的普通字段、文件字段（`format: binary`）、必填字段及 encoding
信息均保留在接口详情中。因此 `docs/rest-project.yaml` 可直接作为导入文件；导入后可
浏览全部接口及其上传定义。其他请求体媒体类型仍返回定位明确的错误。

业务请求失败（包括 YAML 校验失败）会写入 `application.log` 和 `error.log`，记录
`operation`、路由模板、HTTP 状态、`code`、`detail`、`issues` 以及 `request_id`。
`access.log` 记录请求耗时、路由模板和 operationId。内部异常额外记录异常类型及
调用栈的文件、函数、行号；不记录上传全文、请求头、查询参数、局部变量或异常原文。
使用响应头 `X-Request-ID` 的值在日志目录中搜索，即可关联同一次请求：

```bash
rg '响应头中的请求ID' /var/service/log/auto-test-service/*.log
```
