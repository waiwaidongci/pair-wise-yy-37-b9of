# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8314
```

默认端口为`8314`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 分时容量调度

园区压减多家设施负荷，治理设备检修、现场检查、排污许可和审计记录各记一套，汇入同一张分时容量台。

- 容量台按`设施 + 时段(slot)`唯一，同一设施同一时段只占一份减排容量。
- 容量依据分四类：`maintenance`(治理设备检修)、`inspection`(现场检查)、`permit`(排污许可)、`audit`(审计记录)，各自独立记录。
- 检修或检查结论更新后：未执行指令(`planned`)按新依据重算；已下达指令(`issued`)保留并标记`pending_supplement`(待补依据)。
- 抢占容量采用事务串行化：两名监管员同时抢占同一时段，先到者占用，后到者在冲突响应中看到剩余容量(`remaining`)。
- 调度写入失败后按批次恢复：批次以`batch_key`幂等，重提不重复占容量。
- 旧库缺容量依据时，容量台升级为`pending_verification`(待核验基线)，不报错。

容量相关接口：

- `POST /api/facilities`、`GET /api/facilities`、`GET /api/facilities/{id}`
- `POST /api/facilities/{id}/basis`、`GET /api/facilities/{id}/basis?kind=maintenance`
- `POST /api/basis/{id}/conclusion`（可带`capacity`作为新依据容量）
- `GET /api/facilities/{id}/capacity?slot=2026-10-06T08`（缺依据时返回待核验基线）
- `GET /api/facilities/{id}/boards`、`GET /api/facilities/{id}/instructions?status=planned`
- `POST /api/facilities/{id}/seize`（body: `slot`、`amount`）
- `POST /api/batches`（body: `batch_key`、`facility_id`、`slot`、`amount`）
- `GET /api/batches/{key}`、`POST /api/batches/{key}/recover`
- `POST /api/facilities/migrate`（将无依据设施的容量台升级为待核验基线）

允许角色：设施与迁移由`compliance_manager`操作；依据记录与结论更新、抢占与批次调度由`inspector`和`compliance_manager`操作；查看对所有角色开放。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
