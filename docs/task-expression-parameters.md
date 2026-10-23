# 执行时表达式参数：未来 EXPIRES

`taskStep/compute` 在节点实际执行时生成参数。导入、保存、复制、排队和生成 XML 时均不求值；重新运行重新计算；本任务声明为 expression 的参数不使用上一任务组批次的同名旧值替代本次计算。TC-055 的未来 EXPIRES 依据实际执行时刻，不能将生成日计算结果写成 fixed。

```xml
<taskStep order="10">
  <compute timezone="Asia/Shanghai">
    <parameter valueType="string">
      <name>EXPIRES</name><value>http_date(add_seconds(now(), 3600))</value>
    </parameter>
  </compute>
</taskStep>
```

表达式可通过 `param("name")` 引用 fixed、REST 提取或前序 compute 已赋值的参数。计算结果使用普通参数绑定传入请求头、path/query/body、鉴权或动态等待。完整示例见 [task-expression-parameters.xml](examples/task-expression-parameters.xml)。

## 时钟与日期定义

每个 compute 节点在开始计算时捕获一个当前时刻；该节点的所有 `now()` 和 `today()` 共用此时刻，避免跨零点产生不一致。时刻不是 task.created_at、启动请求时间或 XML 生成时间。节点使用 `timezone` 指定的 IANA 时区，默认 `Asia/Shanghai`，不依赖宿主机本地时区；UTC 使用 `UTC`。不同节点分别捕获各自实际执行时刻。


`http_date(add_seconds(now(), 3600))` 生成计算时刻后一小时的 HTTP-date，固定使用英文与 GMT，例如 `Wed, ... GMT`，而不是生成时预填日期。若接口要求 ISO 8601 或 Unix 时间戳，分别使用 `isoformat(...)`、`timestamp(...)` 或 `timestamp_ms(...)`。应将 compute 放在使用 EXPIRES 的请求前，避免中间长时间等待让结果过期。

## 允许的表达式

解释器直接遍历受限 AST，不调用 Python eval/exec。只允许标量字面量、下表函数及有界数值的 `+ - * /` 和一元正负号；括号可用于分组。禁止属性、下标、导入、模块、文件、网络、任意函数、方法调用、推导式、lambda、循环、幂运算和动态参数名。

| 函数 | 结果与约定 |
| --- | --- |
| `concat(s1, ...)` | 拼接 1～100 个字符串，结果最多 65536 字符；非字符串须先用 str 转换 |
| `str(value)` | 标量转字符串，boolean 使用 true/false；拒绝 null、对象和数组，结果最多 65536 字符 |
| `param("name")` | 读取节点执行前的已赋参数；名称必须为字面字符串 |
| `generateUUID()` | 生成标准带连字符 UUID v4 字符串，无参数；每次调用生成新值。需要多个 step 共享时，只生成一次并引用该全局参数。任务级参数显式设置 `type="function"`（API 为 `source_type: function`）、`valueType="string"`，value 完整设置为 `generateUUID()`，在每次 task 初始化时生成一次；`fixed` 始终保留字面量 |
| `now()` | 当前时区的带时区 datetime，中间值 |
| `today()` | 当前时区的执行日期，中间值 |
| `date("YYYY-MM-DD")` | 严格日期；传入 date 或 datetime 时取节点时区的日期 |
| `datetime("...Z")` | 解析带时区 ISO 8601 时刻并转到节点时区；拒绝无时区输入 |
| `add_days(value, days)` | 加减整数日；日期使用日历日，datetime 保持本地钟面时间 |
| `add_years(value, years)` | 加减整数年；目标月不足的日期取该月最后一天 |
| `add_seconds(value, seconds)` | 在 UTC 时间线上加减秒，再转回节点时区，跨夏令时仍按实际秒数 |
| `isoformat(value)` | date/datetime 转 ISO 8601 字符串 |
| `http_date(value)` | datetime 转 GMT HTTP-date，秒精度 |
| `timestamp(value)` | datetime 转 Unix 秒整数，向下取整 |
| `timestamp_ms(value)` | datetime 转 Unix 毫秒整数，向下取整 |
| `format_date(value [, format])` | 默认 `%Y-%m-%d`；格式仅允许 `%Y %m %d %H %M %S %f %z %%` 及普通文字 |

date/datetime 仅作为解释器内部中间类型，不能直接存为参数；最终值必须符合 `valueType` 对应的 JSON 类型。函数参数均为位置参数，不接受关键字参数。数值不接受 bool、NaN、无穷大或绝对值大于 10^15 的值；年份偏移最多正负 1000 年，日期偏移最多正负 366000 天，秒偏移最多该天数对应的秒数，日期必须落在 Python date/datetime 可表示范围。

每个表达式最多 2000 字符、128 个 AST 节点、16 层 AST 深度，字符串字面量最多 1024 字符，格式字符串最多 128 字符；每个 compute 最多 100 个参数。不得通过日期参数执行任意 Python。

## 节点、作用域与失败策略

compute 可位于 main、finally 或单层 parallel 的叶子位置，不发 HTTP、不占 HTTP 槽位，也不持有数据库连接进行计算。节点计入原有 1000 节点限制；parallel 仍至少两个子节点，读取同一输入快照，并禁止兄弟节点写同名参数。

compute 内 parameter 新增唯一全局参数声明，`valueType` 默认 string。节点内所有表达式读取同一个执行前快照，不允许互相引用本节点输出；中间结果依赖应拆成前后两个 compute 节点。来源可用性在保存/启动时静态检查，不通过执行表达式探测。

所有计算和结果类型检查成功后一次发布。表达式计算失败、无效日期、除零、超出范围或结果类型不匹配时，节点记 `EXPRESSION_EVALUATION_ERROR`，不发布部分结果；依赖尚未赋值时为 `PARAMETER_NOT_AVAILABLE`。错误不回显参数值、异常原文或凭证。该行为与 REST 响应提取不同：REST 提取不到值仍赋 null，计算到期时间失败不能伪造一个默认结果。主流程失败后仍按现有规则尝试 finally。

条件跳过、条件阻塞、并发屏障及任务组停止策略沿用[任务执行语义](task-execution-semantics.md)。计算结果继承引用参数的敏感标记，也可显式设置 `sensitive="true"`。预览按预览时实际时刻重新计算，不将预览结果缓存到正式运行。

## REST API 映射与持久化

```json
{
  "global_parameters": [
    {"name":"EXPIRES","source_type":"expression","value_type":"string","expression_source":{"step_index":0}}
  ],
  "steps": [{
    "step_type":"compute","execution_scope":"main","position":10,
    "options": {
      "timezone":"Asia/Shanghai",
      "expressions":[
        {"name":"EXPIRES","expression":"http_date(add_seconds(now(), 3600))"}
      ]
    }
  }]
}
```

此片段展示参数与节点字段，完整请求仍需任务名称和 base_url。`expression_source.step_index` 指完整提交数组的下标，保存后映射为稳定 step_id，复制时重映射；与 rest_source 独立，表达式存入节点 options.expressions 并进入运行快照。表达式输出名称必须恰好覆盖该 compute 的全部声明。

新增 `task_parameters.expression_source` JSONB 列；迁移定义已加入 `db/050_task_execution_nodes.xml`，新建表定义同步更新。按当前修改安排，本轮不执行测试或迁移，后续统一验证。

## 字符串拼接

在 compute 中使用 `concat("order-", str(param("id")), "-", param("suffix"))`，将输出参数整体绑定至 header、path/query/body 或 SQL 参数。字符串字面值中的 `{name}` 仍不做局部替换；原始请求体也不插值。
