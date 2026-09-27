# 建筑抗震鉴定与加固排序

依据结构、用途、人员密度和历史缺陷生成鉴定与加固优先级。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量和加固方案规则。
- `src/repository.py`：SQLite建表、事务、版本控制、方案存储和审计链。
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
- `POST /api/items/{id}/records/{rid}/amend`，补改评估事项
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/plans`，提交加固方案及依据的已关闭评估事项
- `GET /api/items/{id}/plans`
- `GET /api/plans/{id}`
- `POST /api/plans/{id}/review`，`decision`为`approve`或`reject`
- `GET /api/audit`

允许角色：assessor, structural_engineer, review_board, viewer。风险分值和人员密度共同影响排序；审核通过前必须完成评估、设计和施工证据登记。

## 加固方案流程

- 项目进入`assessed`后，结构工程师提交加固方案，必须关联本项目已关闭的评估事项；同一项目只允许一份待审方案。
- 评审委员会同意后项目才能进入`design`；驳回必须填写原因。
- 方案换版（提交新版本）或依据的评估事项被补改，原同意立即失效并记录失效原因；待审方案在依据补改后同样失效，需重新提交。
- 已进入`construction`的项目不因方案失效而倒退。
- 项目列表和详情展示当前方案版本、复核人和进入设计的阻挡原因；方案详情展示依据事项快照及其事后补改标记。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
