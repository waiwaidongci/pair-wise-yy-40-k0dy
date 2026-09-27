# 建筑抗震鉴定与加固排序

依据结构、用途、人员密度和历史缺陷生成鉴定与加固优先级。

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
python3 app.py --db ./data.db --port 8317
```

默认端口为`8317`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `PUT /api/records/{id}`，补改评估事项
- `GET /api/items/{id}/plans`，方案版本列表
- `POST /api/items/{id}/plans`，提交加固方案（引用已关闭评估事项id）
- `GET /api/plans/{id}`，方案详情（版本、复核人、阻挡原因、依据事项）
- `PUT /api/plans/{id}/review`，评审委员会同意/驳回，驳回必须填写`reject_reason`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：assessor, structural_engineer, review_board, viewer。风险分值和人员密度共同影响排序；审核通过前必须完成评估、设计和施工证据登记。

### 加固方案闸门

- 加固方案由 structural_engineer 提交，`basis_record_ids` 只能引用本项目已关闭的 `kind=assessment` 评估事项。
- 同一项目同时只保留一份待复核方案（数据库部分唯一索引保证）；评审委员会同意前，`assessed → design` 一律 409 并返回阻挡原因。
- review_board 评审：`approved` 解锁设计；`rejected` 必须写 `reject_reason`。评审带 `expected_version` 乐观锁。
- 方案换版提交，或评审同意后评估事项又被补充登记/补改，原同意立即置为 `invalidated`：仍在设计阶段的项目自动退回 `assessed`；已进入施工（construction）及以后的项目状态不倒退。
- 列表与详情中的 `reinforcement` 字段给出当前版本、方案状态、复核人、驳回/失效原因和阻挡原因；方案列表中每个版本带 `is_current`、`status_label`、`reviewed_by`、`blocking_reason`、`invalid_reason`。
- 规则集中在 `src/rules.py`，SQLite 存储在 `src/repository.py`，权限与用例编排在 `src/service.py`，JSON 路由在 `src/http_api.py`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
