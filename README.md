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
- `src/capacity_rules.py`：分时槽点、四类依据归一并表（取最大值不叠加）。
- `src/ledger_store.py`：设施、依据、指令、批次和槽位占用表及事务化分配。
- `src/ledger_service.py`：容量台用例——重算、抢占、批次恢复、旧库基线。
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

## 分时容量台

治理设备检修（maintenance）、现场检查（inspection）、排污许可（permit）、审计记录（audit）四类依据统一落在同一张分时容量台上，整点为一个槽位。同一设施同一时段四份依据**取最大减排要求，只占一份容量，不叠加**。

接口：

- `POST /api/facilities`，`GET /api/facilities`：登记设施（含总容量）。
- `POST /api/facilities/{id}/bases`：登记四类依据之一，按类型鉴权；`supersedes_id` 可作废旧依据。
- `POST /api/bases/{id}`：更新检修/检查结论（乐观锁 `expected_version`），自动重算受影响槽位。
- `POST /api/plan/refresh`：对时间窗内的未执行计划统一重算。
- `POST /api/dispatch`：批量下达调度指令，携带 `client_token` 和每行 `external_ref`。
- `POST /api/batches/{id}/recover`：写入失败后按批次恢复，可用已落库行或重提 lines。
- `POST /api/directives/{id}/execute`、`POST /api/directives/{id}/supplement-basis`。
- `POST /api/legacy/import`：旧库导入。
- `GET /api/ledger?facility_id=&from=&to=`：容量台视图（总容量、已占、剩余、合并依据、占用指令）。
- `GET /api/directives`、`GET /api/bases`。

行为约定：

- **结论更新**：未执行（planned）指令按新依据重算或撤销；已下达（issued/executed）指令保留容量不重占，依据不足时标 `pending_basis` 待补依据。
- **抢占**：两名监管员同时下达同一时段，先到者占用（事务内串行化），后到者得到 `conflicted` 和 `remaining` 剩余容量。
- **批次恢复**：批次按 `client_token` 识别，行按 `external_ref` 幂等；已下达行重提只回显（`retried:true`）不重复占容量，未执行行重试，整批重放后给出 completed/partial/failed。
- **旧库基线**：旧库记录若当时没有容量依据，落为 `baseline` 指令并占容量，`verified=false` 列入 `pending_verification`；之后依据补齐且覆盖该槽位时自动核验，也可走 supplement-basis 人工补依据。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
