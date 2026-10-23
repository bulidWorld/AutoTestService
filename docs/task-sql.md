# SQL 任务节点

在 `taskStep` 中配置 `sql`，可用于主流程、parallel 子节点及 finally 清理；完整例子见 [task-sql.xml](examples/task-sql.xml)。

```xml
<taskStep order="10">
  <sql driver="postgresql" timeoutSeconds="30">
    <datasource>host=127.0.0.1 port=5432 dbname=business</datasource>
    <username>test_user</username>
    <password>replace-with-your-password</password>
    <statement><![CDATA[UPDATE orders SET status = 'closed' WHERE id = 42]]></statement>
  </sql>
</taskStep>
```

- `driver` 必填：`postgresql` 或 `sqlite`，复用现有依赖，无需新增驱动。
- `datasource` 必填：PostgreSQL 使用 libpq 连接串或 URI，可附加 `sslmode` 等连接选项；SQLite 使用文件路径。
- `username`、`password` 可选，只适用于 PostgreSQL，优先于数据源内同名配置；SQLite 不配置这两个元素。
- `statement` 必填：一条 SQL，可用 CDATA；不插值、不绑定全局参数、不提取查询结果，SELECT 仅用于执行，不返回结果集。
- `timeoutSeconds` 默认 30，范围 1～300；PostgreSQL 分别限制连接和语句等待时间，SQLite 限制锁等待和语句执行时间，因此不是节点总耗时上限。

每个节点创建独立连接与事务，正常完成提交，失败回滚并关闭连接；不要写 BEGIN、COMMIT、ROLLBACK 或需要事务外执行的语句。不同节点和并发子节点之间不共享事务；主流程 SQL 失败后仍尝试 finally。SQL 不占 HTTP 请求槽位，也不计入 REST 请求统计。服务中断不会自动重放节点。

API 使用 `step_type: sql`，配置放在 `options.sql`，字段为 `driver`、`datasource`、`username`、`password`、`statement`、`timeout_seconds`。沿用节点的 `condition` 策略。导入与保存只校验配置，不连接数据源；预览后续参数时会实际执行前序 SQL，正式运行也会执行。

连接信息随任务配置及执行快照保存、读取，包含密码，应限制任务访问权限。执行错误统一为 `SQL_EXECUTION_ERROR`，不回显数据库错误原文或连接凭据。
