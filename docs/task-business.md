# 任务业务实现

`rest-task.yaml` 的 14 个操作通过 `app/repository/rest_task.py` 显式绑定。默认应用同时加载项目与任务契约；合并 OpenAPI 文档时为任务组件添加命名空间，避免两份契约中的同名 Schema 相互覆盖。HTTP 成功状态码来自契约：创建/复制为 201，启动运行为 202，校验及预览为 200。

任务保存、复制、删除和启动在数据库事务中执行，按项目、任务顺序加锁。任务名称与参数名称唯一性、接口及步骤归属、引用有效性由程序校验。全局 REST 参数只保存 `rest_source.step_id` 与 `extract_expression`，复用该步骤的接口版本和绑定。新步骤先保存取得数据库稳定 ID，再配置 REST 参数引用。

启动运行保存完整不可变配置快照，并创建运行参数及运行步骤记录。服务生命周期中的后台调度器通过 PostgreSQL 会话 advisory lock 选出唯一执行协调者，最多同时处理八个独立任务；同一任务的 queued/running 记录均阻止重复启动。每个任务内部顺序执行：先初始化全局参数，再执行未被全局参数引用的步骤。同一个初始化步骤只请求一次，多参数共享响应。

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
