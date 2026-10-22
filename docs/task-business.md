# 任务业务实现

G1～G6 的补充约定、XML/API 映射和边界见 [任务执行语义](task-execution-semantics.md)。显式节点策略保存在 options JSONB 并进入执行快照。执行时日期和未来到期时间使用 [compute 受限表达式参数](task-expression-parameters.md)，不得在 XML 生成时预计算。

`rest-task.yaml` 的任务操作通过 `app/repository/rest_task.py` 显式绑定；参数预览及单次响应解析由 `rest-parameter.yaml` 与 `app/repository/rest_parameter.py` 提供。默认应用加载项目、任务、参数及任务组契约；加载时将跨文件组件引用打包为本地引用，合并 OpenAPI 文档时添加命名空间，避免同名 Schema 相互覆盖。HTTP 成功状态码来自契约：创建/复制为 201，启动运行为 202，校验及预览为 200。

任务保存、复制、删除和启动在数据库事务中执行，按项目 → 任务组 → 任务锁序协调；queued/running（含清理）或活动组预约时禁止修改。steps 为扁平数组，step_type 为 rest/wait/parallel，execution_scope 为 main/finally；position 是同 scope、同父节点唯一的正 int32。parent_step_index 与 rest_source.step_index 都指完整提交数组下标；保存分两遍建立稳定 ID 并映射父引用与参数来源。复制同时重建两类引用，不转换旧配置。

启动事务保存不可变完整执行树和 REST 接口定义，创建全部运行节点与参数，再映射 parent_run_step_id。唯一 advisory lock 协调者最多同时处理八个活动任务；另有服务级八个 HTTP 槽位，独立任务、组成员、parallel 子请求和清理共享。并发子节点使用独立的最多 64 线程池，不与父协调者争用线程；每次短事务创建独立数据库连接，HTTP 与等待期间不持有数据库连接。

执行顺序为固定参数初始化 → 按兄弟 position 的 main 根节点 → 全部 finally → 任务终态。wait 支持固定 1～2592000000 毫秒或参数驱动 duration/until 等待，使用可中断单调时钟，不占 HTTP 槽位。parallel 仅包含 rest/wait/compute，最多八个活动子节点，子节点读取同一冻结上下文；已启动子节点收尾后一次发布参数赋值（包括 null），不受屏障成功与否影响，finally 可引用。任一失败停止领取新节点，已启动节点收尾，主错误按 position 选择；未启动子节点记 PARALLEL_ABORTED，后续主流程记 UPSTREAM_FAILED。REST 参数独立提取，断言失败也保存并发布；字段缺失、类型不匹配、非 JSON 或无可用响应时赋 null，不影响其他参数或步骤判定。参数 status=succeeded 表示赋值完成（可为 null），published 表示上下文可用；未启动来源不发布。

数据库保留执行所需原值，接口返回按敏感标记脱敏，Authorization/Cookie/Set-Cookie 默认隐藏。敏感参数源步骤的响应体整体隐藏，覆盖提取失败尚未保存参数值的情况。更新敏感固定参数时可省略已有同名同类型参数的 value；更新步骤敏感字面绑定时可省略其 value 保留原值。版本变化或绑定方式变化必须重新提供有效值。不要将脱敏占位符作为原值提交。 raw_body.text 是明确例外：配置、快照和 parallel 外请求详情原文返回，不解析、不插值、不脱敏；仅绕过 application/json 请求体语法及 Schema 校验，path/query/header 仍严格校验。空 text 发送零字节。headers 独立于 bindings，bindings 只允许 path/query/body。

项目删除会清理任务和运行历史，有活动运行则拒绝删除。YAML 文件被任何任务步骤引用时禁止删除，包含 REST 全局参数引用的初始化步骤。

## 验证

业务测试使用临时 PostgreSQL schema 和本地模拟 HTTP 服务，测试结束后删除临时 schema，不修改业务表数据。

```bash
AUTOTEST_DB_TESTS=1 .venv/bin/python -m pytest tests/test_task_business.py -q
.venv/bin/ruff check app tests/test_task_business.py
```

部署使用现有 scripts/start.sh/scripts/restart.sh，启动脚本按 XML 同步数据库结构；单独同步使用 bash scripts/db_sync.sh。本次不提供旧 XML、旧绑定、旧快照的兼容或自动转换。


## 接口资产单次测试

首页提供项目、文件、版本及接口选择，可以新建项目、导入 YAML 后填写参数进行单次测试。参数输入按接口 Schema 保留类型，支持 path、query、header、额外认证头和完整 JSON 请求体；可选参数取消勾选后不发送，敏感参数默认用密码输入框。请求体字段定义可展开查看。

`POST /api/v1/projects/{project_id}/yaml-files/{file_id}/interfaces/{interface_id}/test` 在校验通过后执行一次，返回方法、实际 URL、请求和响应的头/体、HTTP 状态、耗时及错误原因。目标非 2xx、连接错误或超时仍以 HTTP 200 返回测试结果，配置错误返回 400，归属不匹配返回 404。方法和路径使用所选择的不可变接口定义；不创建任务或任务运行历史。本次测试仍限 JSON 请求体，multipart 明确拒绝执行。

```bash
AUTOTEST_DB_TESTS=1 .venv/bin/python -m pytest tests/test_interface_test.py -q
```


## REST 参数单次解析

`POST /api/v1/parameters/rest/extract` 接收 `parameter_name`、`value_type`、`extract_expression` 和 `response`。例如 `response={"response_body":"{\"data\":{\"a\":1}}"}`，表达式为 `data.a`、类型为 `integer` 时返回 `value=1`。响应体编码默认 `utf-8`，按流水线规则解析 JSON 并校验提取类型。该接口不访问数据库、不发送 HTTP 请求、不保存配置或运行记录。

无效输入或表达式返回 400；非 JSON 响应、缺少字段、类型不匹配返回 422，并提供错误分类及字段位置，不回显响应原值。

XML 任务导入：`POST /api/v1/projects/{project_id}/tasks/import`，使用 multipart/form-data 的 `file`（.xml，每个最多 5 MiB）和 `base_url` 字段，`file` 可重复 1-20 次一次导入多个任务。格式见 [task.xsd](task.xsd)，完整示例见 [examples/task.xml](examples/task.xml)。步骤按正整数 order 升序执行，method/path 匹配本项目各文件当前版本的唯一接口；缺失或歧义均拒绝。整批原子导入：任一文件失败则全部回滚，错误定位到具体文件（`body.file.N (文件名)`）及 XPath/行号；同名任务返回 409。成功 201 按上传顺序返回任务数组。导入不发送请求，不创建或修改接口资产。

`parameter` 的 type 必须为 fixed；`valueType` 默认为 string，其他类型使用 JSON 字面量。`extractParam` 声明由步骤响应赋值的全局参数，同名参数拒绝。body/kv 支持 key/value 或 key/parameter；引用参数时可省略 key，使用参数名。pathParams/queryParams/body 分别匹配 path/query/JSON 字段，不跨容器推断；kv 禁止 location 属性，嵌套 JSON 使用 Pointer。assertionCode 可省略，也可使用 CDATA 保存 Python 断言入口。入口必须为 `assert_response(responseBody, globalParam, statusCode, headers)`：statusCode 为真实 HTTP 状态码整数，headers 为小写响应头名到字符串列表的映射（保留重复头），业务 code 另行核对。responseBody 在空响应或非 JSON 时为 None；断言结果不影响参数提取和发布；未取到值赋 null。主流程、并发子 REST、清理 REST 均适用。

```bash
curl -X POST http://localhost:8000/api/v1/projects/1/tasks/import \
  -F 'file=@docs/examples/task.xml;type=application/xml' \
  -F 'file=@docs/examples/task2.xml;type=application/xml' \
  -F 'base_url=https://api.example.com'
```

## 多任务组

`rest-task-collection.yaml` 由 `app/repository/rest_task_collection.py` 显式绑定，默认应用自动加载。
旧任务内步骤组功能已移除。任务组属于项目，引用多个本项目任务，每个成员执行完整任务。

使用 `/api/v1/projects/{project_id}/task-collections` 创建、分页查询；
`/{collection_id}` 查询、完整更新或删除。创建示例：

```json
{
  "name": "登录与订单测试",
  "members": [
    {"task_id": 11, "position": 1},
    {"task_id": 12, "position": 2},
    {"task_id": 13, "position": 2}
  ]
}
```

`POST /{collection_id}/runs` 无请求体，返回 202 和组运行 ID。
`GET /{collection_id}/runs` 查询历史，`GET /{collection_id}/runs/{run_id}` 返回成员状态、
对应 task_run_id 与脱敏后的共享参数；成员请求及步骤详情仍使用原任务运行查询接口。
`GET /{collection_id}/runs/{run_id}/overrides` 分页查询覆盖来源。
删除组需要 `confirm=true`，返回 204；组删除后运行详情和历史仍可查询。

批次按 position 升序执行，同批并发并等待全部完成，所有任务共享服务的八个活动任务名额及八个 HTTP 槽位。
同批成员读取相同的上批参数快照，各自初始化本任务 fixed 参数，再按步骤提取 REST 参数。
全部成功后按 task_id 升序合并实际写入，后写覆盖同名参数，即使值相等也记录覆盖。
ERROR 日志包含参数名称、原任务/运行 ID 和新任务/运行 ID；不包含参数原值或新值。
日志使用数据库投递状态补发，允许按 audit_id 识别重启后的重复日志。

参数名称必须已在本任务声明，类型必须在组内一致。已声明 REST 参数可在源步骤执行之前
从上一批继承初值；同批产出的新值不能互相依赖。保存可暂存需要前序批次输入的 REST 绑定；单独启动缺少输入的任务仍返回 400。
断言和绑定在启动前校验，缺失或顺序错误返回 400。
参数敏感标记按组内同名声明保守合并，敏感值与历史请求均按原有规则脱敏。

成员 primary_error 持久化或 cleanup_status 首次失败后立即停止本批未启动成员，已启动成员继续自身主流程与全部清理；全部达到终态后再结束批次。主流程成功但清理失败也使成员及批次失败，整批不发布输出。
外部 HTTP 副作用不回滚、不自动重试。重启将遗留组及其未完成成员标记 EXECUTION_INTERRUPTED。
前提不足使用 PRECONDITION_BLOCKED 及独立 blocked 终态；仍尝试 finally。阻塞批次不启动后续批次，没有失败成员时组终态为 blocked。
从接受到组终态，全部成员均被预约；活动组禁止改删，预约任务禁止改删及重复启动。
被组配置引用的任务须先移除成员再删除；删除项目时清理所有组配置、审计和成员运行历史。

部署前执行 `bash scripts/db_sync.sh`，重启应用加载新增接口与调度逻辑。

```bash
AUTOTEST_DB_TESTS=1 .venv/bin/python -m pytest tests/test_task_collections.py tests/test_task_business.py -q
```

## 清理、并发统计与中断

完整 XML 示例见 [task-execution-nodes.xml](examples/task-execution-nodes.xml)，包含固定参数、等待、并发、非法 JSON 和清理。

finally 只包含根 rest/wait/compute；清理 REST 可声明 extractParam，使用与 main 相同的提取规则。每步赋值后立即发布到任务上下文，供后续清理绑定或断言引用；清理断言失败仍提取，未取到值赋 null。main 不可提前引用 finally 的产出，清理步骤不可引用自身或后续清理步骤尚未产出的值。主流程 HTTP、断言或固定值初始化失败后，进程正常存活时仍按序尝试全部清理。清理缺少已发布输入时以 CLEANUP_INPUT_UNAVAILABLE 失败且不发请求，后续清理继续。清理阶段运行保持 running，不能修改、删除、重复启动或解除组预约。

运行详情的 execution_stage 为 main/finally/finished；primary_error 保留主流程第一失败，cleanup_status 单独记录清理结果。仅清理失败时顶层为 CLEANUP_FAILED，主流程与清理同时失败则保留主错误。任务耗时包括等待、并发排队和全部清理。

只有 parallel 外 REST（包括 finally）写 task_requests，发送前提交记录；并发 REST 先持久化 running 与发送计数，不保存逐请求响应、状态码、头体或耗时。查询只展示 parallel 父节点及 parallel_stats，快照仍保留完整配置树。父统计通过记录锁与子节点终态事务合并；完整响应（包括非 2xx、断言/提取失败）参与 sum/count/avg/min/max，超时、连接失败与 wait 不参与。无完整响应时 sum=0、avg/min/max=null，单位毫秒、三位小数。

运行请求总数为普通请求数加全部 parallel_stats.request_count，因此可以大于请求分页总数。叶子与控制节点分别计数；运行结果按 main/finally、树前序及兄弟 position/id 排序。

参数预览会发送目标前所需的真实请求，等待仍生效，目标在并发组内时等待已启动子节点收尾；目标来源断言失败仍返回提取值，未取到值返回 null；目标可为 main 或 finally REST 叶子；main 目标不执行 finally，finally 目标执行主流程及目标前清理，主流程失败后仍继续至清理目标。不执行目标后步骤，不创建运行或保存配置。

服务停止时中断等待并停止派发新请求；重新取得协调权后将遗留运行记 EXECUTION_INTERRUPTED，未启动节点跳过，不续跑、不重发、不补清理。保留已提交的并发响应统计，不补建请求详情或虚构耗时样本。

```bash
AUTOTEST_DB_TESTS=1 .venv/bin/python -m pytest tests/test_task_execution_nodes.py tests/test_task_business.py tests/test_task_collections.py -q
```

compute 在实际执行时捕获一个当前时刻，默认 Asia/Shanghai，同节点全部表达式共享该时刻与执行前参数快照。参数来源为 expression，expression_source 引用 compute，表达式保存于 options.expressions。全部计算成功并符合 value_type 后原子发布；无效日期或计算错误不发布部分结果，主流程失败后仍进入 finally。compute 不发 HTTP，不占 HTTP 槽位；参数预览同样在预览执行时求值。
