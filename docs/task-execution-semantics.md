# 任务执行语义：G1～G6

本文规定 `task.xsd` 与 REST API 的共同执行语义。执行时日期、未来 EXPIRES 及受限 now/日期函数另见 [表达式参数规范](task-expression-parameters.md)。XML 结构之外的跨字段规则由导入、保存及启动校验执行。API 使用每个节点的 `options` 保存显式执行策略，随任务复制并进入不可变运行快照。普通绑定仍严格检查接口 Schema。

| 缺口 | XML | API / 执行结果 |
| --- | --- | --- |
| G1：HTTP 断言 | 四参数 `assertionCode` | `statusCode`、`headers` 来自真实 HTTP 响应 |
| G2：动态鉴权 | `auth` 或显式 `header` | `options.auth`，逐步生成或省略鉴权 |
| G3：捕获、条件清理、会话更新 | `extractParam`、`updateParam`、taskStep 条件属性 | 独立赋值、缺失为 null，`options.assignments/condition` |
| G4：动态及长时等待 | `wait` 的参数、时刻、偏移属性 | `options.wait` 或固定毫秒，单节点最多 30 天 |
| G5：非法路径/查询 | `rawPath`、`rawQuery` | `options.raw_path/raw_query`，明确隔离校验豁免 |
| G6：阻塞与停止后续用例 | `onFalse="blocked"` 或 `raise Blocked(...)` | `blocked` 终态，任务组停止未启动成员及后续批次 |

## HTTP 状态与响应头

断言入口必须为：

```python
def assert_response(responseBody, globalParam, statusCode, headers):
    return statusCode == 401 and headers.get("www-authenticate") == ["Bearer"]
```

`statusCode` 为 HTTP 状态码整数，业务 `responseBody["code"]` 必须另行核对。`headers` 为小写名称到字符串列表的映射，保留重复头顺序。JSON 响应按 JSON 类型传入，空响应、非 JSON 或二进制响应传入 `None`。连接失败或超时不运行断言。未配置断言时按 HTTP 200～299 判断；配置后由断言返回的 bool 判定。阻塞信号独立于 bool。

## 每一步的鉴权与保留头

动态令牌必须来自 string 参数：

```xml
<auth mode="bearer" parameter="accessToken"/>
```

API 对应 `options.auth = {"mode": "bearer", "parameter": "accessToken"}`。发送前读取该步骤的参数快照，生成 `Authorization: Bearer <accessToken>`；输入必须为非空、不含空白或控制字符的 ASCII 原始令牌，不得预加 `Bearer `。参数自动标记敏感。值不可用时为 `AUTH_TOKEN_UNAVAILABLE`，不发送请求。

缺少鉴权用例使用 `<auth mode="none"/>`，API 为 `{"mode":"none"}`，即使接口声明 Authorization 必填也不注入该头。无项目级隐式鉴权或令牌继承。伪造、过期、空值、错误前缀等用例使用 `<auth mode="header"/>` 并声明恰好一个 `header Authorization`（API mode 为 header）。仅该模式豁免 Authorization 的接口 Schema，保留 string 类型和换行禁令；没有隐式注入或拼接。bearer/none 与显式 Authorization 同时存在时拒绝配置；没有 auth 时显式请求头仍照常严格校验。

保留头名单大小写不敏感：`Host`、`Content-Length`、`Transfer-Encoding`、`Connection`、`Keep-Alive`、`Proxy-Connection`、`TE`、`Trailer`、`Upgrade`、`method`、`url`、`base_url`、`timeout_seconds`。执行器负责主机、长度及传输配置。Authorization 不在保留名单；禁止重复头绑定和换行。普通头绑定及请求体校验保持原规则。

## 独立赋值、条件清理与会话更新

完整响应到达后，执行器在原始响应上独立提取每个参数，然后用步骤执行前的参数快照判定断言。HTTP 非 2xx、断言 False、断言异常、返回类型错误或阻塞信号均不撤销已捕获的参数。字段缺失、类型不符、非 JSON 或无可用响应时，仅对应参数赋 JSON `null`（Python `None`），不将其他参数清空，也不单独使来源步骤失败。

`extractParam` 新增一个唯一的全局参数声明。更新同一会话引用使用 `updateParam`，不重复声明参数：

```xml
<updateParam><name>accessToken</name><value>data.accessToken</value></updateParam>
<updateParam><name>refreshToken</name><value>data.refreshToken</value></updateParam>
```

API 对应 `options.assignments = [{"name":"accessToken","extract_expression":"data.accessToken"}]`。目标必须已声明，沿用其 `value_type` 和敏感标记，允许更新 fixed 初始化的空字符串会话参数。更新未取到值时也覆盖为 null，不自动回退旧令牌。配置 `sensitive="true"` 可保护刷新令牌等不直接用于 Bearer 的凭证。

清理 REST 可同时使用 `extractParam` 和 `updateParam`；每一步赋值立即发布，后续清理读取最新会话。参数运行记录保存最新赋值和最后一次来源请求，`status=succeeded` 表示赋值完成，值可以为 null；步骤本身的失败或阻塞保留在步骤及运行记录中。

条件写在 `taskStep` 上，适用于 rest/wait/compute/parallel：

```xml
<taskStep order="10" ifParameter="refreshToken" ifOperator="notEmpty" onFalse="skip">
  <rest method="POST" path="/auth/refresh">
    <body><kv><parameter>refreshToken</parameter></kv></body>
    <updateParam><name>accessToken</name><value>data.accessToken</value></updateParam>
  </rest>
</taskStep>
```

API 条件为 `options.condition = {"parameter":"refreshToken","operator":"notEmpty","on_false":"skip"}`。参数须已声明，但允许尚未赋值。`notEmpty` 为默认运算，拒绝缺失、null、空字符串、空数组和空对象；`present` 仅检查是否已赋值（null 也算已赋值）；`truthy` 按布尔值判断；`equals` 与 `ifValue` 的 JSON 字面量比较（API 使用 `value`）。

条件未满足时默认 `skip`：节点记为 skipped、错误分类 `CONDITION_NOT_MET`，不发请求、不提取、不覆盖旧会话。用于清理时表示没有资源需要回收。主流程必需前提应明确使用 `onFalse="blocked"`，不能用跳过关键操作后查询成功来代替用例完成。

并发兄弟读取同一输入快照，不允许写同一参数。任一子节点失败或阻塞后停止启动新子节点，等待已启动节点收尾，仍发布它们的赋值供 finally 使用。未启动或条件跳过的来源不赋值。finally 按顺序尝试全部清理，单步失败不阻断其后的补救清理。缺少已发布参数仍不得发请求；若需要跳过，应显式配置条件。null 不豁免后续正常请求的类型校验。

注册预期失败但实际创建账号时，返回的凭证会先被捕获；运行保留 `ASSERTION_FAILED`，finally 可先刷新会话、取得新 accessToken，再注销账号。若确认没有创建账号，条件清理可跳过对应请求。对已确认创建的账号，注销步骤应按账号存在性设条件；此时缺少令牌会使鉴权/清理失败，不能仅按令牌为空把清理判成成功。完整例子见 [task-session-cleanup.xml](examples/task-session-cleanup.xml)。

## 动态等待、绝对时刻与七天等待

固定毫秒与动态参数二选一：

```xml
<!-- next_send_after 为相对秒数 -->
<wait parameter="next_send_after" mode="duration" unit="seconds"/>
<!-- expiresAt 为带时区的 ISO 8601 字符串；再等待一秒以越过边界 -->
<wait parameter="expiresAt" mode="until" unit="iso8601" offsetMilliseconds="1000"/>
<!-- createdAt 为时间戳，expiresIn 为秒数：等待创建时刻 + TTL + 一秒 -->
<wait parameter="createdAt" mode="until" unit="seconds"
      offsetParameter="expiresIn" offsetUnit="seconds" offsetMilliseconds="1000"/>
<!-- 七天为单个节点 -->
<wait milliseconds="604800000"/>
```

API 动态等待为 `options.wait`，字段 `parameter/mode/unit/offset_parameter/offset_unit/offset_milliseconds`。duration 按相对时长计算；until 将 base 作为 Unix 时间戳或带时区 ISO 8601 时刻，计算 `base + 动态偏移 + 固定偏移 - 当前 UTC`。seconds 与 milliseconds 用于数值，iso8601 只适用于 until。动态偏移默认以秒计，固定偏移以毫秒计。计算结果不大于零时立即完成。

固定等待范围 1～2592000000 毫秒，动态实际等待范围 0～2592000000 毫秒，均最多 30 天；超过上限、参数不可用或类型错误为 `WAIT_VALUE_INVALID`，不得猜测或截断时长。实际等待用单调时钟，可被服务停止事件立即中断，不占 HTTP 槽位或数据库连接。单节点七天等待不消耗额外节点，1000 节点上限不变。不隐式重试或循环请求；这里的时间等待不代表轮询成功。服务重启仍按 `EXECUTION_INTERRUPTED` 终结旧运行，不重放请求、不静默恢复七天计时。

## 非法路径与查询的显式豁免

```xml
<rest method="GET" path="/resources/{id}">
  <rawPath>/resources/not-an-integer</rawPath>
  <rawQuery>limit=invalid&amp;unknown=1&amp;limit=-1</rawQuery>
  <assertionCode><![CDATA[
def assert_response(responseBody, globalParam, statusCode, headers):
    return statusCode == 400
]]></assertionCode>
</rest>
```

接口仍由同项目 method/path 原始定义唯一定位。rawPath 是相对于 base_url 路径前缀的原始请求路径，rawQuery 不带前导 `?`，空 rawQuery 表示无查询。API 分别使用 `options.raw_path/raw_query`。这两个位置不经过参数类型、格式、范围或必填校验，可测试未知参数、重复键、缺少参数或错误路径。

仅对应位置获得豁免：rawPath 与 pathParams 互斥，rawQuery 与 queryParams 互斥；没有 raw 配置的位置保持严格校验。输入必须为 ASCII，无空白、控制字符或 `#`，rawPath 必须以 `/` 开头且不能带 `?`。作者负责所需百分号编码，执行器不插值、不二次编码，不改变主机、端口或协议。rawBody 仍只豁免请求体解析与 Schema 校验。

## 阻塞终态、清理结果与串行停止

前提不足使用 taskStep 条件 `onFalse="blocked"`，或在断言中显式 `raise Blocked(...)`。后者的异常文本不回传、不记录响应秘密，错误分类统一为 `PRECONDITION_BLOCKED`。阻塞不是断言 False；节点和运行有独立 `blocked` 状态，统计包含 blocked/blocked_count。阻塞停止后续 main，但仍尝试全部条件清理。

主流程第一错误保留在 primary_error，cleanup_status 单独表示清理结果。主流程阻塞且清理成功或无清理时，运行终态 blocked；清理失败时终态 failed，primary_error 仍保留原阻塞/失败原因，cleanup_status=failed。仅清理失败时 error_code=CLEANUP_FAILED。查询或准备步骤成功不证明目标行为完成，必须为用例目标设置状态、响应及前提断言。

相关用例必须通过同一任务组编排停止策略。严格串行使用不同且递增的成员 position；当前用例完成主流程及全部清理后才启动下一批。前提阻塞、主流程失败或清理失败均停止未启动成员及后续批次；已经启动的成员收尾并执行自身 finally。清理首次失败即发布 cleanup_status=failed，调度器据此停止领取同批未启动成员。失败/阻塞批次不向后续任务发布共享参数；不影响不属于该任务组的独立运行。阻塞批次且无失败成员时，任务组终态 blocked；存在失败成员时为 failed。

## 部署与验证安排

`task_steps.options` 为新增 JSONB 字段，`wait_milliseconds` 改为 bigint。迁移定义在 `db/050_task_execution_nodes.xml`，新建表定义同步更新。上线前执行项目现有数据库同步流程并重启应用。本轮按要求仅修改实现和规范，没有运行测试或数据库迁移；统一测试应覆盖全部 G1～G6、历史字段读写、敏感值与任务组停止路径。
