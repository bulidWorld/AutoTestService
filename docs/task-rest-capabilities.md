# 通过 REST 新增与修改任务能力

任务配置接口支持字符串拼接、SQL 参数绑定和 REST 轮询：

| 操作 | 接口 | 成功状态 |
| --- | --- | --- |
| 新增任务及全部参数、步骤 | `POST /api/v1/projects/{project_id}/tasks` | 201 |
| 修改任务及全部参数、步骤 | `PUT /api/v1/projects/{project_id}/tasks/{task_id}` | 200 |
| 读取保存后的完整配置 | `GET /api/v1/projects/{project_id}/tasks/{task_id}` | 200 |

请求均使用 `Content-Type: application/json`。完整请求见 [task-capabilities.json](examples/task-capabilities.json)，Swagger 的创建和保存任务接口也提供相同示例。

使用示例前，先导入项目的 `GET /orders/{id}` 接口，将 JSON 中的 `file_id`、`interface_id`、`version` 改为实际值；接口的 path 参数 id 应为 integer。示例 SQL 使用 SQLite，需准备 `/tmp/task-orders.db` 和 `orders(id INTEGER, label TEXT)` 表。`base_url` 指向被测服务，和自动化服务 API 地址分别配置。

```bash
curl -X POST 'http://127.0.0.1:8080/api/v1/projects/1/tasks' \
  -H 'Content-Type: application/json' \
  --data-binary @docs/examples/task-capabilities.json
```

请求中的能力映射：

| 能力 | JSON 字段 | 数据库保存位置 |
| --- | --- | --- |
| 字符串拼接 | compute 的 `options.expressions`，例如 `concat("order-", str(param("order_id")))` | `task_steps.options.expressions` |
| 表达式结果声明 | `source_type: expression`，`expression_source.step_index` 或 `step_id` | `task_parameters.expression_source`，保存后统一为稳定 step_id |
| SQL 参数绑定 | sql 的 `options.sql.parameters: ["label", "order_id"]` | `task_steps.options.sql.parameters` |
| REST 轮询 | rest 的 `options.poll`，含 `max_attempts`、`interval_milliseconds`、`timeout_seconds` | `task_steps.options.poll` |

SQL 参数按数组顺序传给驱动；SQLite 使用 `?`，PostgreSQL 使用 `%s`，占位符不加引号。SQL 值不拼接进语句。轮询立即发起首次请求，每次按 assertion_code 判断成功；没有断言时使用 HTTP 2xx。只对 HTTP_STATUS_ERROR 和 ASSERTION_FAILED 继续轮询，最终响应才发布提取参数。详见 [SQL 节点](task-sql.md)和[执行语义](task-execution-semantics.md)。

修改时提交完整 TaskWrite 配置，修改相应 expressions、sql 或 poll 字段即可：

```bash
curl -X PUT 'http://127.0.0.1:8080/api/v1/projects/1/tasks/123' \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/task-update.json
```

`/tmp/task-update.json` 使用与新增请求相同的结构。保留已有步骤时，在步骤对象中携带读取所得的 id；新增步骤不传 id。来源可使用 `step_id`，也可使用本次 steps 数组的 `step_index`，二者只能选一个。省略旧步骤会删除该步骤，省略可选 poll 会取消轮询。GET 响应包含只读元数据，编辑时只提交写入模型字段。

保存是一个数据库事务，非法表达式、未声明 SQL 参数、不可用的前序计算结果或非法轮询范围会返回 400，原配置保持不变。作为任务组继承输入的 REST 参数沿用已有草稿规则，独立启动仍校验实际可用性。活动运行或任务组预约期间修改返回 409。保存不执行表达式、SQL 或业务 HTTP 请求；正式运行在 `POST .../tasks/{task_id}/runs` 启动，每次运行冻结当时的完整配置，后续修改不覆盖历史快照。

数据库继续使用现有 JSONB 列，不新增重复存储的字段。部署执行 `bash scripts/db_sync.sh`，并重启服务加载更新的 REST 契约和执行器。
