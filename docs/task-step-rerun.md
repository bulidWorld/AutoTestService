# 单步骤重复执行：接口与数据库设计

本轮只定义 REST 契约和数据库字段，不实现执行器、页面或数据库部署。
契约位于 `docs/ rest-api/rest-task.yaml`；结构位于 `db/070_step_reruns.xml`。
未绑定的操作沿用现有 501 返回，不能视为功能已上线。

## 执行范围

源运行必须已经结束，目标步骤状态为 succeeded/failed/blocked，且有完整实际输入快照。
支持 main/finally 的 rest、wait、compute 叶子，包括 parallel 内部叶子；不支持 parallel 父节点。
步骤在构造实际输入前失败、被条件跳过或没有执行的，不允许重复执行。
parallel 子节点暂未在现有详情展开，后续页面支持需单独定义；本接口仍严格检查运行步骤归属。

重复执行只执行该叶子，不启动前置步骤、参数来源请求、后续步骤或 finally。
原请求可能再次产生副作用；每次 POST 都创建新的记录，不提供自动重试。
不接受参数覆盖；再次重执行始终选择原步骤，不支持以重复执行记录为来源。
原运行状态、请求计数、parallel 汇总、参数和任务组上下文保持原结果。

## REST

统一前缀：`/api/v1/projects/{project_id}/tasks/{task_id}/runs/{run_id}/steps/{run_step_id}`。
这里的 run_step_id 是 task_run_steps.id，不是 task_steps.id。

| 方法 | 路径后缀 | 返回 |
| --- | --- | --- |
| POST | `/reruns` | 无请求体；202 StepRerun，初始 queued |
| GET | `/reruns?page=1&page_size=20` | 200 StepRerunPage，created_at/id 降序 |
| GET | `/reruns/{rerun_id}` | 200 StepRerun，查询独立状态和结果 |

资源不存在或任一层归属不匹配返回 404。以下冲突返回 409，使用既有 Error 结构：

| code | 条件 |
| --- | --- |
| SOURCE_RUN_NOT_FINISHED | 源任务运行尚未结束 |
| STEP_NOT_EXECUTED | 目标步骤未实际执行或不是允许的终态 |
| STEP_TYPE_NOT_SUPPORTED | parallel 父节点 |
| STEP_INPUT_SNAPSHOT_UNAVAILABLE | 历史无快照、输入构造失败或快照版本不支持 |
| TASK_EXECUTION_CONFLICT | 同任务存在活动运行、重复执行或被活动任务组预约 |

已接受执行的 HTTP、断言、提取等失败写入独立记录，GET 仍返回 200；服务重启对 queued/running
记录标记 failed / EXECUTION_INTERRUPTED，不自动补发。敏感信息沿用现有脱敏和响应大小限制。

## 实际输入快照

新增 task_run_steps.input_snapshot JSONB，可空。与已有 task_runs.config_snapshot 分工：
后者保存定义，前者在目标步骤执行前保存当时实际输入。禁止从运行最终 global_parameters 推导。
历史 NULL 不回填，不退回到当前配置解析。

快照格式 v1（内部存储，不作为 API 返回结构）：

| 字段 | 内容 |
| --- | --- |
| version | 固定 1 |
| captured_at | 实际输入冻结时间 |
| step_type / execution_scope / in_parallel | 叶子类型、main/finally 及是否属于 parallel |
| step_definition | 原运行配置中的目标步骤，含断言、提取、条件及更新参数策略 |
| values | 步骤执行前可读取的实际参数名值映射，保留 JSON 类型与 null；缺失键表示不可用 |
| sensitive_names | 敏感参数名集合，派生值传播规则沿用现有实现 |
| resolved_request | rest 的 method、最终 url、保留重复头的 headers 名值数组、body 文本或 null、timeout_seconds；包含已解析鉴权 |
| resolved_wait_milliseconds | wait 的已解析等待时长，其他类型为 null |
| evaluation_time / timezone | compute 的原求值时间和时区；时间函数复用该时间，不能读取重复执行时的当前时间 |
| resolved_options | 条件判断、动态鉴权、原始路径查询等策略已解析的输入/决定，不再次全局求值 |

先完成实际输入构造，再以短事务持久化快照和步骤运行/发送意图，提交后才发送或等待。
HTTP 超时、响应断言失败仍有可重放快照；输入构造失败则保持 NULL，不能重复执行。
重复执行从该快照直接发送 resolved_request，或等待已冻结时长；断言和提取使用原定义与 values 的私有副本。
compute 复用原输入和 evaluation_time 计算，updateParam 仅更新私有副本，产出保存在 output_parameters。
必须保存未脱敏实际值才能复用 token 等输入，API 和日志不暴露原始 input_snapshot；
过期 token 仍原样使用，鉴权失败为本次结果，不刷新 token 或重跑前置步骤。

## 独立记录和事务

新增 task_step_reruns，字段、默认值及索引以 XML 为准。
input_snapshot 在接受事务中复制源快照并保持不可变，输出字段与状态可以随执行更新。
每次有独立 run_uuid；发送 X-Task-Step-Run-UUID 和日志关联使用新 UUID。
request_result 仅保存 parallel 外 REST 的脱敏响应摘要；不写入原 task_requests。
parallel 内叶子仍不保存逐请求响应或 duration_ms，不改变现有并发记录边界。

不设置外键或 CHECK，服务负责归属、快照版本和状态校验。后续实现必须让普通启动、任务组启动、
重复执行及删除复用项目→任务组→任务锁序；同事务检查 task_runs、组预约及 task_step_reruns 活动态，
防止交叉启动。重复执行排队期间也占用任务活动资格，并共享现有调度上限和全局 HTTP 槽位。
任务编辑、删除和项目清理需一并检查活动重复执行；删除历史先清理关联 reruns，再清理源运行。

XML 随既有 db 同步流程加载，幂等新增列和表；本轮不执行数据库同步。
