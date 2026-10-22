# 扩展任务 XML 与执行模型设计

## 1. 目标与设计范围

细化 [spec.md](spec.md) 中的四项能力：等待、任务内并发、发送非法原始 JSON，以及正常执行失败后仍尝试清理。
交付范围包括 XSD、示例 XML、OpenAPI 契约、数据库结构、导入转换、任务管理、执行调度和运行结果查询。
本文为待实施设计，不表示相应功能已经实现。本次仅生成设计文档。

根据用户要求，直接采用统一的新任务模型，不设计旧 XML、旧配置和历史快照的兼容、版本分流或自动转换机制。
数据库结构同步仍遵守项目 XML 变更规范；结构同步与旧任务数据兼容是两个独立问题。

需求未指定的边界在本文明确约定：等待使用固定时长；并发使用单层屏障；清理使用任务级 finally；
原始请求体只支持 UTF-8 文本，不提供模板插值。循环、条件分支、轮询重试、任意脚本、嵌套并发、二进制请求体和页面编辑器不在本次范围。

## 2. 当前实现与改动依据

| 位置 | 当前行为 | 本次调整 |
| --- | --- | --- |
| `docs/task.xsd` | 每个 taskStep 只有一个 rest，order 在整个任务内唯一 | 引入 rest/wait/parallel 三种节点及 finally 容器 |
| `app/service/task_import.py` | 排序后逐个查找 rest，转换为扁平步骤；body/kv 混合绑定路径、查询和请求体字段 | 解析执行树，按独立参数容器转换绑定，生成带父子关系的统一步骤配置 |
| `app/service/task_config.py` | build_request 校验 JSON Schema，execution_plan 顺序展开 REST 与参数 | 按节点类型校验、分析依赖，增加原始请求体构造 |
| `app/repository/rest_task.py` | 所有步骤必须绑定接口，保存和复制只映射参数来源 | 按类型保存、查询、复制，映射父节点与参数来源 |
| `app/service/task_execution.py` | RunExecutor 顺序执行，异常直接 finish_failure | 增加并发屏障、等待、清理阶段和延迟终结 |
| `app/service/task_collections.py` | 基于线性 execution_plan 判断可用参数 | 使用统一执行树依赖分析，等待成员清理结束再终结批次 |
| `db/020_task_compose.xml` | task_steps 的接口字段非空，运行步骤没有树结构 | 调整字段可空性，补充节点配置与运行层级 |

现有请求发送器已经将 request_body 编码为 UTF-8，并自动计算 Content-Length，可复用这一层发送原始文本。
不能复用其上游“所有请求体必须符合接口 JSON Schema”的假设。

## 3. XML 格式

### 3.1 总体结构

无命名空间，继续使用 task、parameters、taskStep、rest、extractParam 等标签。
根节点子元素顺序为：可选 parameters、至少一个主流程 taskStep、可选 finally。
finally 最多出现一次；出现时至少包含一个 taskStep。根 task 的 name 及导入表单 base_url 含义不变。

每个 taskStep 必须且只能包含 rest、wait、parallel 中的一种。order 必填，范围为 1～2147483647，
在同一父容器中唯一，允许间断，按数值升序解释。不同容器可以重复使用相同 order。

| 节点 | 属性和子元素 | 执行含义 |
| --- | --- | --- |
| rest | method、path；header*、pathParams?、queryParams?、(body 或 rawBody)?、assertionCode?、extractParam* | 一次 HTTP 请求及其断言、参数提取 |
| wait | 固定 milliseconds 1～2592000000 或动态 parameter/mode/unit/offset | 依据响应值计算 duration/until，不发请求、不生成参数 |
| parallel | 可选 maxConcurrency，整数 1～8，默认 1；至少两个 taskStep | 子节点并发，全部结束形成屏障；REST 仅保存汇总统计，不保存逐请求响应 |
| finally | 一个或多个 taskStep，只允许 rest 或 wait | 主流程结束后按 order 顺序尝试所有清理步骤 |

parallel 只允许直接包含 rest 或 wait 子步骤，不支持嵌套 parallel，也不表示项目级 task_collections。
并发子步骤的 order 用于稳定展示、排队与错误选择，不表示完成顺序。
finally 中不允许 parallel 或子 finally；清理 REST 可声明 extractParam，提取结果供后续清理步骤使用，断言失败不影响赋值，未取到值赋 null。
任务总节点数上限为 1000，计入 parallel 父节点及全部清理节点；业务层统一校验。

REST 参数按用途放入独立容器，`pathParams`、`queryParams`、`body` 均为 `rest` 的直接子元素：

请求头使用独立的 `header` 标签，与上述容器同级，可重复出现；每个 header 包含 name、value，并保留可选 valueType。
例如 `<header><name>X-Client</name><value>demo</value></header>`；value 为 `{client}` 时引用全局参数 client。
header 不使用 kv 或 location 属性；请求头名称不区分大小写且不得重复，仍执行现有的类型、换行和执行器保留头校验。

| 元素 | 内容与约束 |
| --- | --- |
| pathParams | 路径参数绑定，仅匹配接口路径占位符，例如 `/resources/{id}` 中的 `id` |
| queryParams | URL 查询参数绑定，仅匹配接口定义中的 query 参数，例如 `?page=1` 中的 `page` |
| body | 结构化 JSON 请求体字段绑定，仅匹配请求体 Schema 中的字段 |
| rawBody | 原始 JSON 请求体文本，与 body 二选一 |

前三个容器均可省略或为空，最多各出现一次，内部使用零到多个 `kv`。
`kv` 保留可选 `key`、`value` 与 `parameter` 二选一、可选 `valueType` 的结构；字面值必须提供 key，
引用参数时可省略 key，以参数名作为目标字段名。移除 XML `kv` 的 `location` 属性，绑定位置由所在容器确定，不再跨位置推断。
`body/kv` 的普通 key 表示 JSON 顶层字段，JSON Pointer 表示嵌套字段；pathParams/queryParams 的 key 按参数名精确匹配。
同一容器内禁止重复绑定同一字段，body 中也禁止同时绑定父字段及其子字段；不同容器可使用相同 key。
路径和查询参数的必填项、类型及请求体 Schema 仍按接口定义校验。
旧的 `<body><kv location="path|query">…</kv></body>` 写法不再接受，body 中也不再根据同名路径或查询参数推断绑定位置。

### 3.2 原始 JSON 请求体

在 rest 中增加 `rawBody`，内容为 xs:string，不提供属性。
采用 CDATA 表达非法 JSON，例如 `<![CDATA[{"name":]]>`。
`body` 和 `rawBody` 不能同时出现，即使 body 为空也不允许。
配置 `rawBody` 时，仍可通过同级的 `pathParams` 和 `queryParams` 分别绑定路径参数和查询参数。

原始模式遵循以下规则：

- rawBody 元素存在即启用原始模式；空元素表示发送零字节请求体，区别于未配置请求体。
- 发送 XML 解析后的文本，UTF-8 编码，不去除首尾空白，不做 JSON 解析、重新序列化或参数替换。
  `{token}`、`${token}` 等均作为普通文本。XML 本身的换行归一化仍会发生，不承诺与源文件字节完全一致。
- 接口仍按同项目 method/path 唯一匹配并固定版本，且必须声明 application/json 请求体。
  仅绕过该请求体的 JSON 语法、必填字段和 Schema 校验；path/query/header、地址、归属、超时仍严格校验。
- 默认补充 Content-Type: application/json，显式配置也只能为 application/json。
  Host、Content-Length 等头仍由执行器控制。
- 允许发送合法或非法 JSON，是否符合测试预期由 assertionCode 判断。未配置断言时仍使用 HTTP 2xx 规则。
  断言继续接收响应 JSON；非 JSON 响应会使配置了业务断言的步骤失败，本次不扩展断言入口。
- 非 parallel 子节点的 REST 由 request_body 保存实际发送文本；parallel 内不创建逐请求详情，原始体配置仍保留在配置及快照中。
  配置、快照及已有请求详情查询直接返回原文，不对 rawBody 做脱敏，错误及日志也不增加针对 rawBody 的脱敏处理。
  修改时直接提交新的文本，无脱敏占位符或敏感文本专用编辑规则。

### 3.3 完整示例

以下接口均须先导入同一项目；POST /validate 的请求体和响应体定义需支持相应测试。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<task name="并发与异常请求测试">
  <parameters>
    <parameter type="fixed"><name>client</name><value>demo</value></parameter>
  </parameters>
  <taskStep order="10">
    <rest method="POST" path="/resources">
      <extractParam valueType="integer"><name>resourceId</name><value>data.id</value></extractParam>
    </rest>
  </taskStep>
  <taskStep order="20"><wait milliseconds="1000"/></taskStep>
  <taskStep order="30">
    <parallel maxConcurrency="2">
      <taskStep order="1">
        <rest method="GET" path="/resources/{id}">
          <header><name>X-Client</name><value>{client}</value></header>
          <pathParams><kv><key>id</key><parameter>resourceId</parameter></kv></pathParams>
        </rest>
      </taskStep>
      <taskStep order="2">
        <rest method="POST" path="/validate">
          <rawBody><![CDATA[{"name":]]></rawBody>
          <assertionCode><![CDATA[
def assert_response(responseBody, globalParam, statusCode, headers):
    return statusCode == 400 and responseBody["code"] == "INVALID_JSON"
]]></assertionCode>
        </rest>
      </taskStep>
    </parallel>
  </taskStep>
  <finally>
    <taskStep order="10">
      <rest method="DELETE" path="/resources/{id}">
        <pathParams><kv><key>id</key><parameter>resourceId</parameter></kv></pathParams>
      </rest>
    </taskStep>
    <taskStep order="20"><rest method="POST" path="/test-session/close"/></taskStep>
  </finally>
</task>
```

即使并发中的校验请求失败，仍等待已启动子步骤结束，再执行 DELETE 和关闭会话。
若创建资源失败而没有 resourceId，DELETE 标记失败且不发送请求，关闭会话仍会执行。

### 3.4 XSD 与业务校验分工

沿用 lxml 的 XSD 1.0 能力，提取命名类型 RestStep、WaitStep、ParallelStep、MainTaskStep、LeafTaskStep、CleanupTaskStep。
MainTaskStep 使用 xs:choice；LeafTaskStep 排除 parallel；CleanupTaskStep 的 REST 类型排除 extractParam。
rest 子元素固定顺序为 header*、pathParams?、queryParams?、可选的 body/rawBody 二选一、assertionCode?、extractParam*。
使用 xs:choice 在 XSD 层保证 body 与 rawBody 互斥；pathParams、queryParams、body 复用不含 location 属性的 kv 类型。
业务层仅在对应容器指定的位置匹配字段，检查接口定义、字段类型、重复绑定及 JSON 父子字段冲突，不再跨位置推断。

根节点的 uniqueOrder 只选择主流程 taskStep；parallel 和 finally 各定义本容器的 order 唯一约束。
XSD 检查结构、枚举、整数边界及兄弟 order 唯一；业务层补充节点总量、名称、接口匹配、绑定类型、
父子归属、原始模式互斥和参数可用性。不能把 xs:assert 等 XSD 1.1 特性写入现有校验器。
所有新增元素、属性及类型必须有中文 xs:documentation，说明默认值、失败行为、合法与非法例子，便于模型生成 XML。
继续禁止 DTD、实体和外部引用，上传限制为 5 MiB，导入不发送 HTTP 请求。

## 4. 统一配置与存储

### 4.1 任务 API 配置

任务读写仍使用扁平 steps 数组，节点关系由父引用表示。每个步骤新增：

| 字段 | 含义与约束 |
| --- | --- |
| step_type | 必填 rest / wait / parallel |
| execution_scope | 必填 main / finally |
| position | 必填兄弟排序值，正 int32，同一 scope、parent 下唯一 |
| parent_step_id | 已保存 parallel 父步骤 ID；根节点不提供 |
| parent_step_index | 本次 steps 数组的父下标，从 0 开始；与 parent_step_id 二选一，仅写入时支持 |
| wait_milliseconds | wait 必填，其他类型禁止配置 |
| max_concurrency | parallel 必填，XML 省略时转换为 8，其他类型禁止配置 |
| headers | REST 请求头绑定数组，元素仅含 name、binding；省略时为空数组，不使用 location 字段 |
| raw_body | REST 可选对象，仅包含 text:string |

REST 继续使用 file_id、interface_id、version、bindings、assertion_code，新增独立 headers；wait/parallel 禁止携带这些字段及 raw_body。
XML 的 pathParams/kv、queryParams/kv、body/kv 分别转换为 bindings 中 location 为 path、query、body 的绑定。
API bindings 的 location 仅允许 path/query/body，请求头不再进入 bindings。
XML header 单独转换为 headers 数组元素；binding 复用现有 literal/global 结构，分别保存按 valueType 解析的字面值或全局参数引用。
例如 `<header><name>X-Client</name><value>{client}</value></header>` 转换为
`headers: [{"name":"X-Client","binding":{"kind":"global","parameter_name":"client"}}]`。
保存、复制、快照、运行查询、依赖分析及请求构造均独立处理 headers，不再转换回 location=header 绑定；提交旧绑定写法时拒绝。
raw_body 与任何 location=body 的绑定互斥，但可与 path/query 绑定及 headers 共存。
parent 只能指向同任务、main scope 的根 parallel；子节点必须为 main 的 rest/wait。
finally 的节点必须是根 rest/wait。禁止自引用、跨任务引用、循环和非法深度。
steps 数组顺序只定义临时 step_index，调度以 position 与父子关系为准。
读取只返回稳定 parent_step_id，根节点返回 null；不返回 parent_step_index。

参数 rest_source.step_index 仍指向整个提交数组的下标，不能误解为兄弟 position。
保存时先建立全部临时节点映射，再统一映射父引用及参数来源；复制任务同时重建这两类 ID。
XML 按主流程前序遍历、再清理流程展开数组，XML order 原值写入 position。

### 4.2 数据库

在原表定义中更新最终结构，并新增 `db/050_task_execution_nodes.xml`，保存可重复执行的结构升级语句。
新增文件必须加入当前数据库同步入口扫描范围；不维护独立手写建表脚本。
沿用 integer identity 主键、JSONB、timestamptz，不加外键和 CHECK，关系及取值由业务事务校验。

| 表 | 字段调整 |
| --- | --- |
| task_steps | 新增 step_type varchar(16)、execution_scope varchar(16)、parent_step_id integer nullable、wait_milliseconds integer nullable、max_concurrency integer nullable、headers jsonb 非空且默认空数组、raw_body jsonb nullable；interface_id/file_id/version 改为 nullable；bindings 不再保存请求头 |
| task_run_steps | 新增 step_type、execution_scope、parent_run_step_id integer nullable、parallel_stats jsonb nullable；根节点的父引用为空；position 改为兄弟序号语义；parallel_stats 仅 parallel 父节点使用 |
| task_run_parameters | 新增 published boolean 非空，默认 false，区分提取成功与已发布到本任务执行上下文 |
| task_runs | 新增 execution_stage varchar(16)，取 main/finally/finished；primary_error_code、primary_error_message；cleanup_status varchar(16)，取 pending/running/succeeded/failed/skipped |

新增节点类型、scope 与运行阶段字段在最终结构中均非空；raw_body 只保存 text。
等待时长、并发上限、独立 headers 及原始体等执行配置进入不可变 config_snapshot；运行记录不重复保存整份配置。
新增索引：task_steps(task_id, execution_scope, parent_step_id, position, id)、
task_run_steps(run_id, execution_scope, parent_run_step_id, position, id)。

不新增请求表。wait/parallel 及 parallel 内的 REST 子节点均不创建 task_requests。
只有 parallel 外的 REST（包括清理 REST）创建逐请求记录，关联对应叶子运行步骤。
parallel 子节点的 task_run_steps 仅保存调度所需的状态、生命周期时间及内部错误分类，不保存请求详情、响应状态码、响应头、响应体或逐请求响应耗时。
parallel 父节点在 parallel_stats 保存汇总计数与响应时间统计，字段及口径见 5.2.1。
task_requests.phase 新增 finally，主流程继续按是否为参数来源区分 parameters/steps，
phase 只用于展示分类，不能决定实际执行顺序。参数来源可为 main 或 finally scope 的 REST 叶子；finally 产出仅在该清理步骤赋值后可用。

启动事务创建完整快照、全部运行节点及参数结果，并在第二遍写入运行父 ID。
运行查询使用运行节点及快照恢复层级，不依赖当前配置存在。
节点数量统计分别返回叶子统计和控制节点统计，避免 parallel 父节点与子节点重复计算请求或执行进度。
任务请求总数由普通 task_requests 数量加各 parallel_stats.request_count 得到，不能仅统计请求表。
运行步骤展示按 scope（main、finally）、树前序遍历、兄弟 position/id 排序；不能仅按平铺 position 排序。

## 5. 执行与参数语义

### 5.1 主流程和等待

执行次序为：固定参数初始化 → 主流程 → 清理流程 → 任务终态。
主流程根节点按 position 顺序执行；普通 REST 完成断言及全部参数提取后才进入下一节点。
等待使用单调时钟计算耗时，实际等待不得短于目标时长，调度延迟可以使其更长。
等待期间不持有数据库事务、连接或 HTTP 槽位；响应服务停止信号，不使用不可中断的长 sleep。
配置的 timeout_seconds 仅限制单个 HTTP 请求，不限制 wait 或整个 parallel。

每个 REST 执行一次，不自动重试、不跟随重定向。
parallel 外先持久化请求记录再发送；parallel 内先持久化子节点 running 状态及请求计数再调用发送器，不保存逐请求详情。
同一步骤的多个 extractParam 独立解析与校验，在同一事务保存并发布赋值；未取到值或类型不匹配时仅该参数赋 null，其他值保留；断言失败不影响提取和发布。
HTTP 或配置断言决定该叶子的最终成功状态；参数独立赋值，原值或 null 均保存，参数 status=succeeded 表示赋值完成，与叶子断言结果无关。

### 5.2 并发屏障

parallel 开始时冻结当前参数上下文，每个子节点使用独立副本。
子节点之间不可读取对方本轮提取的新值；不得共享可变 values、responses、数据库连接或事务对象。
全部子节点成功才在屏障提交各子节点写集，后续主流程及清理可以读取这些值。
任务内参数名仍唯一，每个参数只有一个 fixed 或 REST 来源，因此并发写集不允许出现同名声明。
屏障提交将本组参数的 published 一次性置为 true；主流程普通 REST 在赋值完成事务中置为 true，不受断言结果影响；fixed 在初始化成功事务中置为 true。
published 只表示本任务内可见，向项目级任务组共享上下文发布仍取决于成员及整个批次最终成功。

任一子节点失败，停止领取尚未启动的子节点，并将其标记 skipped；已经开始的请求和等待继续收尾。
等待所有已启动子节点结束后，parallel 标记 failed，本组已赋参数（包括 null）仍发布到任务上下文供 finally 使用，后续主流程根节点跳过。
子节点响应仅在内存中用于断言和参数提取，处理完成后释放，不写入请求表、运行快照、错误正文或日志。
提取出的参数值仍作为 task_run_parameters 保留，失败屏障下标注未发布，不能用这些结果重建运行上下文。
参数查询增加 published:boolean，fixed 初始化成功或所属执行节点赋值发布后为 true；失败屏障也在已启动节点收尾后发布赋值；未启动来源仍为 false。
parallel 的主错误选择 position 最小的失败子节点，避免由线程完成顺序决定错误；父节点仅保存该子节点 ID、错误分类及不含响应内容的错误摘要。
其他子节点仅保留内部错误分类供调度及计数使用，查询不返回逐接口响应或错误详情。

#### 5.2.1 并发响应统计

parallel 的运行查询只展示父节点状态、整体耗时、错误摘要和 parallel_stats，不提供 REST 子请求详情。
配置快照仍包含完整执行树，内部子节点状态用于恢复和调度；查询不展开这些子节点的运行结果。
断言与参数提取继续执行，保存提取结果不代表保存整份响应。统计覆盖该 parallel 的全部 REST 子节点，wait 不计入请求或响应时间统计。

| parallel_stats 字段 | 统计口径 |
| --- | --- |
| rest_total | 配置中的 REST 子节点总数，不含 wait |
| pending_count / running_count | 尚未开始、正在执行的 REST 子节点数 |
| succeeded_count / failed_count / skipped_count | 按断言及参数提取完成后的最终状态计数；发送前失败也计入 failed_count |
| request_count | 已持久化发送意图并调用发送流程的次数；进程中断时不能保证对端实际收到请求 |
| response_count | 收到完整 HTTP 响应的请求数，包含非 2xx、断言失败及提取失败；超时、连接失败和响应未收完整不计入 |
| response_time_sum_ms | 上述完整响应的耗时之和，用于计算平均值，不保存各次耗时明细 |
| avg_response_time_ms | response_time_sum_ms / response_count |
| max_response_time_ms / min_response_time_ms | 上述完整响应中的最大、最小耗时 |

响应耗时使用单调时钟，从调用 HTTP 发送器开始到响应体接收完成为止，包含连接建立及网络收发，
不包含并发排队、HTTP 槽位等待、wait、断言、参数提取和数据库持久化。单位为毫秒，返回数值保留三位小数。
response_count=0 时，sum 为 0，平均、最大和最小值为 null；失败和未启动请求不得作为零耗时样本参与计算。
REST 状态计数之和始终等于 rest_total；成功数不等于响应数，例如完整收到响应但断言失败时只增加响应数和失败数。
parallel 自身仍记录包含排队、wait 和收尾的墙钟耗时，与响应时间统计分开。

子节点完成时，将内部终态、参数提取结果及父节点统计增量在同一短事务内提交；
使用父记录锁或原子更新串行合并 sum/count/min/max，以子节点从运行态转为终态保证每个结果只合并一次。
运行中查询返回截至当前已提交的累计统计；完成后为最终值。
进程中断后保留已提交统计，不从缺失的响应明细重算，也不将未完成请求补成响应样本；相关子节点归入失败或跳过计数。

### 5.3 全局调度上限

保留任务协调者最多同时处理八个活动任务的限制，额外设置服务级八个 HTTP 请求槽位。
独立任务、任务组成员、并发子请求和清理请求共享该 HTTP 槽位；parallel 的 maxConcurrency 限制本节点同时活动的子步骤。
wait 计入本 parallel 的活动子步骤数，但不占 HTTP 槽位。
HTTP 槽位从准备持久化请求记录或并发子节点发送状态，到响应处理及对应记录或汇总统计写入完成一直持有，所有退出路径必须 finally 释放。
排队期间叶子保持 pending，获得并发许可和 HTTP 槽位后才进入 running，并在发送前再次检查主流程失败/停止信号。

父协调线程不持有 HTTP 槽位，也不能与子请求使用同一批会被父线程占满的工作线程池。
采用独立的有界子节点执行池，最多 64 个工作线程（八任务乘以八子步骤），配合全局 HTTP 信号量；
所有子任务只在线程入口创建独立数据库连接，数据库连接仅在短事务内持有。
禁止每个 parallel 私建无界线程池。该 HTTP 槽位由持有 advisory lock 的唯一调度者统一管理。

### 5.4 依赖校验

导入、保存和启动复用同一树形依赖分析，不再分别推导线性 execution_plan。

1. 固定参数初始化成功后可用；主流程按顺序传播保证已发布的参数集合。
2. parallel 所有子节点从相同输入集合分析，不能因先遍历某子节点就令其输出对其他子节点可用。
3. 并发屏障收尾后输出集合为已启动子节点赋值的并集（含 null），不受屏障成功与否影响。当前任务不得重复声明同名来源。
4. wait/parallel 不能作为参数提取源；finally REST 可声明参数提取源，其产出供后续清理引用。
5. 清理绑定只要求参数已声明、类型及来源合法，因为主流程可能在任何位置失败；实际发送前检查本次值是否已发布可用。
6. bindings、headers 中的参数引用和 assertionCode 均纳入读取依赖分析。断言动态读取整个 globalParam 时保守要求全部声明参数可用，沿用现有策略。

保存草稿仍可声明需要任务组前序批次输入的 REST 参数；单任务启动必须证明输入由本任务提供，
任务组启动必须证明由严格前序批次或本任务此前节点提供。并发节点中的“此前”指屏障之前，不能指其他并发子节点。
清理在运行时若缺少绑定或断言所需参数，该清理节点失败，error_code=CLEANUP_INPUT_UNAVAILABLE，不发送请求，继续下一清理节点。
任务组的继承初值仍可使用；失败提取和失败屏障不得覆盖该初值。

## 6. 清理与任务终态

### 6.1 清理入口

启动前静态校验失败时不创建运行，也不执行清理。
已经进入 running 的任务，无论固定参数初始化失败、主流程 HTTP/断言/提取失败、等待或调度内部错误，
只要执行进程和持久化能力正常，都进入 finally。主流程正常成功也进入 finally。
主流程异常先保存 primary_error，不立即调用 finish_failure 终结整个运行。
没有 finally 时 cleanup_status=skipped，直接计算终态。

清理读取固定值、主流程已发布赋值及前序清理提取结果；失败 REST、失败 parallel 已产生的赋值仍可用，未取到值为 null，未启动来源不可用。
清理步骤按 position 顺序执行；单步失败不阻断后续清理。每步仍遵守请求超时、不重试、先记录后发送规则。
所有清理尝试结束后才释放任务运行互斥和任务组预约，运行状态在此之前一直保持 running。

### 6.2 结果归并

| 主流程 | 清理 | 任务最终结果 |
| --- | --- | --- |
| 成功 | 全成功或未配置 | succeeded |
| 失败 | 全成功或未配置 | failed，保留主流程错误 |
| 成功 | 任意失败 | failed，顶层 CLEANUP_FAILED，详情列出清理错误 |
| 失败 | 任意失败 | failed，顶层保留主流程错误，cleanup_status=failed |

primary_error 专门保存主流程错误；清理各自错误保存于运行节点，不覆盖第一失败原因。
多个清理失败时全部保留，按 position 稳定展示。
主流程跳过节点使用 UPSTREAM_FAILED，未启动的并发节点使用 PARALLEL_ABORTED。
任务耗时包括等待、并发排队、全部清理；parallel 耗时为墙钟耗时，不是子节点耗时之和。

### 6.3 中断边界

“失败后必执行”指执行进程正常存活时保证尝试所有清理步骤，不能承诺断电、进程被杀或数据库不可用时仍完成清理。
服务停止时中断等待、停止派发新请求；恢复协调权后对遗留 queued/running 运行标记 EXECUTION_INTERRUPTED，
运行中节点记 failed，pending 节点及未尝试清理记 skipped，不续跑、不补发清理请求。
清理中断时 cleanup_status=failed；尚未进入清理时 cleanup_status=skipped。
中断前已经终态的节点结果及并发汇总统计保留；已有的未完成请求记录补齐中断结果。
parallel 内仅更新子节点状态及父节点计数，不补建逐请求记录、不虚构响应时间样本。不得重复发送已记录发送意图但发送结果不确定的请求。

## 7. 与任务组、管理操作的衔接

任务内 parallel 与项目级任务组分别控制子步骤和成员任务，共享全局 HTTP 限额。
成员主流程失败后进入清理期间，task_runs 仍为 running；组协调者必须同时检查已持久化 primary_error，
据此停止启动本批未开始成员，不能等清理结束才发现本批失败。
已经运行的其他成员继续完成其自身主流程及清理。
只有本批全部已启动成员达到最终态后，组协调者才判定该批结果。

成员只有主流程和清理全部成功才允许发布其已发布写集（含清理产出）；清理失败同样使本批失败，整批不发布。
清理 REST 可提取参数，成员及批次最终成功后与其他已发布参数一并合并并记录覆盖审计；任务组现有确定性覆盖、敏感标记传播及失败隔离规则继续适用。
组启动的可用输出分析只计入成员成功时保证发布的参数。

保存、复制、删除及启动复用项目 → 任务组 → 任务的固定锁序。
保存禁止修改有 queued/running 运行或被活动任务组预约的任务，清理阶段也属于活动运行。
接口资产删除检查只查询 REST 节点，但同时覆盖主流程和清理流程引用。
复制同时映射 parent_step_id 和 rest_source.step_id；项目删除显式清理全部节点与运行记录。

## 8. 接口契约与错误

先修改 `docs/ rest-api/rest-task.yaml`，再修改 repository/service，不新增硬编码 FastAPI 路由。
更新 TaskStep 输入/详情、任务复制、ConfigSnapshot、RunStep、RunDetail、RequestDetail 及导入操作说明；
使用按 step_type 区分的 oneOf，跨字段关系在业务层补充校验。每个字段提供 description 和 example。
`rest-parameter.yaml` 的预览流程同步使用执行树，执行到目标前所必需的屏障，等待仍生效；
预览不创建任务运行。main 目标不执行 finally；finally 目标执行必要主流程及目标前清理，主流程失败后仍继续清理至目标。目标来源断言失败仍返回提取值，未取到值返回 null。契约需明确预览会发送必要的真实请求。
`rest-task-collection.yaml` 更新成员终态、清理失败、HTTP 总限额及输出依赖说明。

沿用现有导入及运行路径，导入成功 201、启动成功 202、格式/配置/依赖失败 400、归属不存在 404、
名称/活动运行冲突 409、文件超限 413；已接受运行的业务失败从运行查询返回，不变为启动接口同步错误。
运行详情新增 execution_stage、primary_error、cleanup_status 及节点层级，保留任务 queued/running/succeeded/failed 状态集合。
RunStep 的 parallel 节点返回 parallel_stats 及整体耗时，不展开内部子节点运行结果；参数查询仍可查看提取值及 published 状态。
请求分页仅包含 parallel 外的 REST，按 phase（parameters、steps、finally）、position、id 稳定排序；树内位置从 run_step_id 关联查看。
RequestDetail 不提供 parallel 子请求详情；接口说明明确此类请求只在父节点统计中展示，请求总数与详情列表条数可不同。

| 新增错误分类 | 触发条件 |
| --- | --- |
| INVALID_STEP_STRUCTURE | 节点类型、字段组合、层级、order 或节点上限不合法 |
| RAW_BODY_CONFLICT | rawBody 与结构化 JSON 体混用，或目标接口不支持 JSON 体 |
| PARAMETER_NOT_AVAILABLE | 主流程绑定或断言依赖尚未发布的参数 |
| PARALLEL_FAILED | 并发节点失败，详情保留确定选择的失败子节点 ID 及原始分类 |
| PARALLEL_ABORTED | 因同组失败而没有启动的并发子节点 |
| CLEANUP_INPUT_UNAVAILABLE | 清理参数缺失，未发送请求 |
| CLEANUP_FAILED | 主流程成功但清理失败 |

校验 issues 定位 XML XPath/行号或 API steps 数组字段；例如
`/task/taskStep[@order='30']/parallel/taskStep[@order='2']/rest/rawBody`。
XML Schema 错误不能继续仅返回不含定位的通用提示；输出结构位置和具体错误原因。

## 9. 实施顺序与验证

1. 完成 OpenAPI、XSD、示例和数据库 XML；检查引用、注释及类型约束一致。
2. 实现统一节点校验、依赖分析和 XML 转换，更新保存、复制、删除、快照及查询。
3. 改造调度器与执行器，落实全局 HTTP 限额、屏障写集、等待及清理终结。
4. 同步参数预览和任务组分析/调度，更新 `docs/task-business.md` 与运行说明。
5. 用临时 PostgreSQL schema 和本地模拟 HTTP 服务验收，不依赖真实业务接口。

| 编号 | 验证场景 | 预期 |
| --- | --- | --- |
| XSD-01 | 三类节点、finally、跨容器相同 order | 按新结构导入，层级及排序正确 |
| XSD-02 | 同级重复 order、空 parallel、嵌套并发、字段混用、kv 携带 location、body 与 rawBody 同时出现（含空 body） | 拒绝并定位，无任务或步骤部分落库 |
| XML-01 | pathParams、queryParams、body 绑定同名字段，body 使用嵌套 JSON Pointer | 按各容器的位置匹配，转换为对应 API bindings，不跨位置推断 |
| XML-02 | 仅存在于 path/query 的字段放入 body，或容器内重复绑定、body 父子字段同时绑定 | 拒绝并定位具体容器与 kv |
| HDR-01 | 独立 header 配置字面值及参数引用，保存、复制并运行 | API、存储及快照使用独立 headers，依赖校验有效，实际发送的请求头值正确 |
| HDR-02 | API bindings 使用 location=header、header 名称大小写重复或携带非法值 | 拒绝并定位，请求头不再通过 bindings 配置 |
| WAIT-01 | 顺序等待和并发等待，服务停止 | 时长不提前结束，无 HTTP 记录，停止可响应 |
| PAR-01 | 两个用测试屏障同步的慢请求 | 确认请求执行区间重叠，后续步骤在二者结束后执行 |
| PAR-02 | 同批参数相互引用或重复声明 | 保存/启动拒绝，无 HTTP 发出 |
| PAR-03 | 一个子步骤失败、其他已启动或尚未启动 | 已启动收尾、未启动跳过、主流程后续跳过，之后开始清理 |
| PAR-04 | 成功子步骤提取值但另一子步骤失败 | 参数历史保留值且 published=false，不保存原始响应，后续及清理不读取失败屏障写集 |
| PAR-05 | 多任务组及独立任务均有并发和清理 | 所有实际 HTTP 在途请求最多八个，无父子线程池死锁 |
| PAR-06 | 并发请求返回不同响应耗时及非 2xx，部分断言或提取失败 | sum/count/avg/min/max 口径正确，失败的完整响应仍计入耗时，wait 不计入 |
| PAR-07 | 全部超时或连接失败、全部未发送、部分请求未启动 | 无完整响应时平均/最大/最小为 null；失败、跳过计数准确，不混入零耗时样本 |
| PAR-08 | 并发节点运行、复制及查询，请求体与响应含可识别文本 | 仅父节点展示汇总，无子请求详情、响应内容或逐请求响应耗时落库及写入日志；提取参数按规则保留 |
| PAR-09 | 同时完成、重复完成回调、运行中查询和中断恢复 | 汇总不丢失、不重复，状态计数一致；恢复保留已提交值，不伪造响应样本 |
| RAW-01 | 非法 JSON、空字符串、空白、中文、字面 {token} | 模拟服务收到 XML 解析文本的 UTF-8 字节，无序列化或插值 |
| RAW-02 | rawBody 与同级 pathParams/queryParams 共存；再加入 body | 前者可执行，后者拒绝；path/query/header 校验仍有效 |
| RAW-03 | 400 响应 JSON 由 assertionCode 判定符合预期 | 步骤成功，非法 JSON 测试不会固定以非 2xx 失败 |
| RAW-04 | rawBody 保存、复制、修改及运行查询 | raw_body 仅含 text，配置/快照保留原文，修改后发送新文本；parallel 外请求详情保留原文，parallel 内只展示统计 |
| FIN-01 | 主流程成功或 HTTP/断言/提取失败 | 均尝试全部清理，整体终态符合归并表 |
| FIN-02 | 清理依赖参数未生成，后续清理存在 | 当前节点失败且无请求，后续清理继续 |
| FIN-03 | 两个清理均失败，同时主流程失败 | 三处错误保留，顶层主错误稳定，运行结束前不能重复启动 |
| FIN-04 | 固定参数初始化运行时失败 | 仍进入清理；无可用输入的清理明确失败，其他清理继续 |
| RUN-01 | 清理过程中重启或等待期间停止 | 中断结果完整，不续跑、不重发、不补发清理 |
| RUN-02 | 复制含并发及清理的任务，再查看运行 | 父节点及参数源指向新 ID；配置树、阶段、并发统计及普通 REST 请求关联准确 |
| COL-01 | 成员主流程失败且清理缓慢 | 即时停止未启动同批成员；等待已启动成员清理；整批写集不发布 |

验证采用 `tests/test_task_import.py`、`tests/test_task_business.py`、`tests/test_task_collections.py` 的现有测试基础，
补充专门的执行树及调度用例。并发测试使用模拟服务屏障和在途计数验证真实重叠及限额，避免仅用总耗时推断并发。
运行 XML/XSD、OpenAPI 引用与契约校验，以及受影响的任务、参数、任务组业务测试；完成后再同步部署结构并重启服务。

## 后续 G1～G6 语义补充

以 [任务执行语义](../../../docs/task-execution-semantics.md) 的显式策略为准：options 持久化到节点与运行快照；新增动态 Bearer、条件跳过/阻塞、updateParam 会话更新、最长 30 天动态等待、原始路径查询和任务组失败停止规则。阻塞为独立终态 blocked，单次清理失败立即停止任务组领取后续用例，所有已启动清理仍收尾。本次未运行测试或迁移。

## 执行日表达式参数

新增 compute 节点及 expression 参数来源；实际执行时捕获 now，按显式 IANA 时区计算日期和未来 EXPIRES，保存/生成/复制时不求值。详细语法、边界及 API 映射见 [表达式参数规范](../../../docs/task-expression-parameters.md)。
