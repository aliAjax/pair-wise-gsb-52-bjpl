# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、同意、服务履约、复查期限和计划版本和冲突检查。
- `src/repository.py`：SQLite建表、事务、乐观并发和服务批次查询。
- `src/service.py`：用例编排、权限检查、批次幂等恢复和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与依据链测试。

## 依据链（计划版本 → 同意 → 履约 → 复查）

每条记录都有独立于乐观锁 `version` 的 `plan_version`，从 v1 起递增：

- **监护人同意**（`consent`）挂接当时的 `plan_version`（`consent_basis_version`）；激活（`activate`）同样记录依据版本。
- **服务履约**：`log_service` 登记的条目为 `pending`，按当前额度判定，记录 `basis_version/basis_minutes`；`confirm_services` 确认后固化 `confirmed_basis_version/confirmed_basis_minutes` 快照。
- **服务时长调整**（`adjust_plan`，仅 case_manager）：`plan_version` 加一，所有未确认条目转为 `void` 并按新额度重新计算（`pending_minutes/voided_minutes/missing_minutes/compliance_rate`）；**已确认条目保留确认当时的快照，不回溯、不改写**。
- **复查**（`review`/`recheck`，仅 administrator）把结论（达标/未达标）与当时的计划版本、分钟快照一起固化在 `reviews[]` 中；计划再调整或出现新的履约证据后，原结论继续可见但标记为 `stale`，结束计划（`close`）要求存在与当前版本一致的现行复查。
- 两个老师同时提交同一计划时，乐观锁只放行先到者；后到者收到 409，响应 `details` 中带 `expected_version/plan_version/current_service_minutes`，刷新后先看到新依据。

### 服务批次与失败恢复

`POST /api/records/{id}/service-batches`，请求体：

```json
{"batch_id":"b-001","expected_version":6,"items":[
  {"ref":"s1","session_minutes":60,"provider":"SP-1"},
  {"ref":"s2","session_minutes":90,"provider":"SP-2"}]}
```

- 每个条目以 `batch_id + ref` 为幂等键；批次进度（已应用键）持久化在 `service_batches` 表。
- 写入中途失败后，用**同一个完整批次**重试：只补未完成条目，已落库/已记账的条目跳过，服务分钟不会重复累计；全部完成后同一批次再重放返回零新增。
- 响应含 `state(processing/completed/failed)`、`applied_items`、`applied_keys`、`recovered`、`last_error`。

### 统计与审计

- `GET /api/stats`：除状态计数 `states` 外，新增 `plan_versions` 汇总和 `records[]`，逐条给出采用的 `plan_version`、同意依据版本、最近复查版本及各服务条目的确认/待确认/失效数量和版本集合。
- `GET /api/records/{id}/audit`：每个动作的 `details.basis` 都带 `plan_version/service_minutes/record_version`；`details.event` 带登记、确认、调整、复查的具体条目与快照。


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
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。动作包括`consent/activate/log_service/confirm_services/adjust_plan/review/recheck/amend/close`。
- `POST /api/records/{id}/service-batches`：服务条目批次提交与失败恢复，请求体为`{"batch_id":"...","expected_version":1,"items":[...]}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
