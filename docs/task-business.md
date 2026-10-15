# 任务业务实现

`rest-task.yaml` 的任务操作通过 `app/repository/rest_task.py` 显式绑定；参数预览及单次响应解析由 `rest-parameter.yaml` 与 `app/repository/rest_parameter.py` 提供。默认应用加载项目、任务、参数及步骤组契约；加载时将跨文件组件引用打包为本地引用，合并 OpenAPI 文档时添加命名空间，避免同名 Schema 相互覆盖。HTTP 成功状态码来自契约：创建/复制为 201，启动运行为 202，校验及预览为 200。

任务保存、复制、删除和启动在数据库事务中执行，按项目、任务顺序加锁。任务名称与参数名称唯一性、接口及步骤归属、引用有效性由程序校验。全局 REST 参数只保存 `rest_source.step_id` 与 `extract_expression`，复用该步骤的接口版本和绑定。新步骤与 REST 参数可同时提交：rest_source.step_index 使用本次提交 steps 数组从 0 开始的下标，与 step_id 二选一。服务先校验完整配置，再在同一事务保存步骤并将引用转换为数据库稳定 step_id；返回及持久化配置只包含 step_id。

启动运行保存完整不可变配置快照，并创建运行参数及运行步骤记录。服务生命周期中的后台调度器通过 PostgreSQL 会话 advisory lock 选出唯一执行协调者，最多同时处理八个独立任务；同一任务的 queued/running 记录均阻止重复启动。每个任务内部顺序执行：先初始化固定全局参数，再按 task_steps 顺序逐步发送请求；每个步骤完成后执行其绑定表达式并赋值，后续步骤可引用已赋值的参数。同一步骤只请求一次，多参数共享响应。禁止引用当前或后续步骤尚未赋值的参数。步骤绑定复用 task_parameters：name 保存参数名称，rest_source.step_id 保存步骤 ID，rest_source.extract_expression 保存 data.a 等取值表达式；运行值仅存运行记录。

请求记录先提交再发送。执行不自动重试或跟随重定向，HTTP 2xx 成功；其他状态、超时、连接错误、非 JSON 初始化响应、提取缺值或类型错误分别记录原因。失败后未执行的参数和步骤跳过。关闭浏览器不影响后台执行。服务重新取得协调权时将启动前遗留的 queued/running 运行标记为执行中断，不续跑。

数据库保留执行所需原值，接口返回按敏感标记脱敏，Authorization/Cookie/Set-Cookie 默认隐藏。敏感参数源步骤的响应体整体隐藏，覆盖提取失败尚未保存参数值的情况。更新敏感固定参数时可省略已有同名同类型参数的 value；更新步骤敏感字面绑定时可省略其 value 保留原值。版本变化或绑定方式变化必须重新提供有效值。不要将脱敏占位符作为原值提交。

项目删除会清理任务和运行历史，有活动运行则拒绝删除。YAML 文件被任何任务步骤引用时禁止删除，包含 REST 全局参数引用的初始化步骤。

## 验证

业务测试使用临时 PostgreSQL schema 和本地模拟 HTTP 服务，测试结束后删除临时 schema，不修改业务表数据。

```bash
AUTOTEST_DB_TESTS=1 .venv/bin/python -m pytest tests/test_task_business.py -q
.venv/bin/ruff check app tests/test_task_business.py
```

部署使用现有 `scripts/start.sh`/`scripts/restart.sh`，启动脚本会按 XML 同步数据库结构。单独同步可使用 `bash scripts/db_sync.sh`。既有独立接口形式的 REST 参数配置需要编辑后重新绑定具体任务步骤，不能自动推断对应步骤。


## 接口资产单次测试

首页提供项目、文件、版本及接口选择，可以新建项目、导入 YAML 后填写参数进行单次测试。参数输入按接口 Schema 保留类型，支持 path、query、header、额外认证头和完整 JSON 请求体；可选参数取消勾选后不发送，敏感参数默认用密码输入框。请求体字段定义可展开查看。

`POST /api/v1/projects/{project_id}/yaml-files/{file_id}/interfaces/{interface_id}/test` 在校验通过后执行一次，返回方法、实际 URL、请求和响应的头/体、HTTP 状态、耗时及错误原因。目标非 2xx、连接错误或超时仍以 HTTP 200 返回测试结果，配置错误返回 400，归属不匹配返回 404。方法和路径使用所选择的不可变接口定义；不创建任务或任务运行历史。本次测试仍限 JSON 请求体，multipart 明确拒绝执行。

```bash
AUTOTEST_DB_TESTS=1 .venv/bin/python -m pytest tests/test_interface_test.py -q
```


## REST 参数单次解析

`POST /api/v1/parameters/rest/extract` 接收 `parameter_name`、`value_type`、`extract_expression` 和 `response`。例如 `response={"response_body":"{\"data\":{\"a\":1}}"}`，表达式为 `data.a`、类型为 `integer` 时返回 `value=1`。响应体编码默认 `utf-8`，按流水线规则解析 JSON 并校验提取类型。该接口不访问数据库、不发送 HTTP 请求、不保存配置或运行记录。

无效输入或表达式返回 400；非 JSON 响应、缺少字段、类型不匹配返回 422，并提供错误分类及字段位置，不回显响应原值。

XML 任务导入：`POST /api/v1/projects/{project_id}/tasks/import`，使用 multipart/form-data 的 `file`（.xml，最多 5 MiB）和 `base_url` 字段。格式见 [task.xsd](task.xsd)，完整示例见 [examples/task.xml](examples/task.xml)。步骤按正整数 order 升序执行，method/path 匹配本项目各文件当前版本的唯一接口；缺失或歧义均拒绝。同名任务返回 409，所有校验通过才原子保存。导入不发送请求，不创建或修改接口资产。

`parameter` 的 type 必须为 fixed；`valueType` 默认为 string，其他类型使用 JSON 字面量。`extractParam` 声明由步骤响应赋值的全局参数，同名参数拒绝。body/kv 支持 key/value 或 key/parameter；引用参数时可省略 key，使用参数名。key 自动匹配 path/query/JSON 顶层字段，歧义通过 location 指定，嵌套 JSON 使用 Pointer。assertionCode 可省略，也可使用 CDATA 保存现有 Python 断言入口。

```bash
curl -X POST http://localhost:8000/api/v1/projects/1/tasks/import \
  -F 'file=@docs/examples/task.xml;type=application/xml' \
  -F 'base_url=https://api.example.com'
```

## 步骤组

`rest-group.yaml` 通过 `app/repository/rest_group.py` 实现组管理和运行。组属于任务，
同一步骤可属于多个组；成员数组决定组内执行顺序。名称去除首尾空白后任务内唯一，
允许保存空组，运行空组返回 400。删除组需要 `confirm=true`，成功返回 204 空响应。

组运行复用任务运行执行器、队列及运行查询接口。启动时锁定项目及任务，整任务和组运行
共享任务的 queued/running 互斥。组管理写操作也拒绝在任务活动运行期间执行。
组快照包含组名、有序成员和接口配置；查询运行详情时 `config_snapshot.group` 返回组快照，
`run.group_id` 标识来源。删除或修改组不改变运行快照，整任务运行的 `group_id` 为 null。

组运行先初始化固定参数，只保留组内请求及断言所需的 REST 参数。所有组内步骤的绑定均
参与依赖校验，参数源步骤必须在组内并排在使用者之前；缺源、逆序或循环依赖在创建运行前
返回 400，不额外执行组外步骤。Python 断言支持静态识别 `globalParam["name"]` 和
`globalParam.get("name")`；动态键、遍历或传递整个参数字典时，保守地检查全部声明参数。
断言在当前步骤提取参数之前执行，不能使用当前或后续步骤才产生的参数。

复制任务同时复制组并映射到新步骤 ID；任务配置移除步骤时清理成员引用并重排组内顺序。
删除任务或项目会清理组及成员，保留单独删除组之前产生的运行历史。
