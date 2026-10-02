# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、同意、服务履约、复查期限、计划版本、依据链重算和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8328
```

默认端口为`8328`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计与每条记录采用的依据版本。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 依据链与版本

支持计划、监护人同意、服务履约和复查通过`basis_version`接成依据链：同意记录`consent_basis_version`；每条服务记录保存`basis_version`与当时额度快照`quota`；复查结论`review_conclusion`携带所依据的版本。

- `log_service`支持单条或批量提交：`{"batch_id":"...","entries":[{"entry_id":"...","session_minutes":60,"provider":"..."}]}`。写入失败后重交完整批次即可恢复：已存在的`entry_id`自动跳过，只补未完成记录，服务分钟不会重复累计；整批校验失败时不写入任何记录。
- `confirm_service`确认服务记录：`{"entry_ids":["..."]}`或`{"confirm_all":true}`，确认时冻结当时快照。
- `amend`提交`service_minutes`会提升`basis_version`：未确认的服务记录失效并按新额度重新计算（放得下则重挂到新版本，放不下则标记`invalid`并停止计入，额度恢复后重新计算可再次生效）；已确认记录保留当时快照，分钟数始终计入。旧复查结论移入`superseded_review`并置`review_stale`，直到下一次复查。
- 两人同时提交同一计划时，版本冲突或状态冲突的响应带有`details`（当前版本、状态、依据版本和服务时长），后到者能立即看到新依据。
- `GET /api/stats`返回`states`、`basis_versions`和每条记录采用的`version`/`basis_version`；审计时间线每个事件的`details`都带有`basis_version`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
