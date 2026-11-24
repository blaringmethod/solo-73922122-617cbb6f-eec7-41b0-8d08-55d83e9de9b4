# 煤质数据仓 · 旧煤样重判入仓服务

按**现行规则版本**重判旧煤样并把每批**最后一次成功结果**入仓 SQLite，供下游按批号查询的 FastAPI 服务。

## 业务规则

- 测值为 `0 ~ 10000` 的整数**万分比**；水分、灰分均给出闭区间 `[lo, hi]`。
- 每批最多 **100** 条记录；每个规则版本最多 **20** 条规则。
- 规则按 `seq` 顺序判定，取**首条同时命中两个闭区间**的等级；全部未命中 => **未分级**（JSON 中用 `null` 表示）。
- 旧档按批提交并带**原规则版号**；重判时先从旧档区间内筛出"按原版能得出原等级"的测值对：
  - 现行版下等级**唯一** => 直接定级（未分级也是一种唯一定级）；
  - 出现**多个等级** => 列出每个等级、一对可行测值（见证点）及该等级的可行测值对总数；
  - 原等级**无解** => 报样号并**退回整批**（HTTP 422），不覆盖历史成功结果。
- 重判期间仍可发布并启用新版本；重判开始时对启用版本拍快照（版本 id、版号、换版纪元），
  **入仓写事务内复核换版纪元**：从读取现行版号到提交前只要**实际换过版**
  （含 `v2→v3→v2` 这类又切回快照版的情形），整批退回（HTTP 409），
  下游继续查到上一份成功结果及其所用版号 `judged_version`。
- 什么才算"实际换版"：仅当启用目标与当前启用版是**另一个版号**时才记一次换版
  （`activation_log.switched=1`，推进单调换版纪元）。**只发布不启用**、
  **重启用现行版**（`switched=0`）都不推进纪元，进行中的重判可照常入仓。
- 409 响应与退回记录同口径，均给出重判用版 `rejudge_version`、入仓复核时版号
  `review_version`（在写事务内读取，避免提交后再查误报）以及期间换版链路
  `switch_chain`（如 `["v2","v3","v2"]`）。

## 核心算法：坐标压缩，不枚举测值域

规则是两个轴上闭区间的笛卡尔积矩形。把两版规则的全部边界（`lo`、`hi+1`）加上
`0`、`10001` 排序去重，相邻切点之间形成一维均匀格，水分格 × 灰分格即二维均匀格——
同一格内任一点在任一版规则下的命中结果完全相同。

只遍历与旧档矩形相交的格：

1. 格代表点（夹到交集内）分别用原版、现行版定级；
2. 原版定级 ≠ 原等级的格丢弃；
3. 其余格按现行版等级聚合，格覆盖的整数点数（两轴交集长度之积）累加为该等级的可行对数。

复杂度只随**规则边界数**（每版 ≤20 条规则 => 每轴 ≤41 个切点）与**记录数**（≤100）增长，
与测值域宽度无关，全程不枚举 `0..10000`。单条记录最坏约 `40×40=1600` 个格，
实测 100 条全域记录整批约 26 ms（见下文实测）。

## 目录结构

```
app/
  models.py    # Pydantic 请求/响应模型与入参校验
  rejudge.py   # 坐标压缩核心算法（纯函数，可独立测试）
  db.py        # SQLite 持久层（WAL、换版纪元守卫事务、启用审计、退回审计、旧库迁移）
  service.py   # 重判编排（快照 → 逐样求解 → 无解退回 / 漂移退回 / 入仓）
  main.py      # FastAPI 路由，启动自动播种 v1/v2 演示版本并启用 v2
tests/         # pytest：算法对拍（300 组随机暴力枚举）、服务、HTTP 端到端、旧库迁移、多进程守卫
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务: http://localhost:8000
# 交互文档（可直接演示重判）: http://localhost:8000/docs
```

SQLite 数据文件位于命名卷 `coal-data` 的 `/data/coal.db`（WAL 模式）。

### 本地直接运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 库路径可用环境变量 COAL_DB_PATH 覆盖，默认 data/coal.db
```

首次启动若库内无版本，自动播种：

| 版号 | 规则 | 说明 |
| --- | --- | --- |
| `v1` | 优质煤 `m≤800,a≤2000`；普通煤 `m≤1200,a≤3000`；等外煤全域兜底 | 旧版 |
| `v2` | 优质煤 `m≤700,a≤1500`；普通煤 `m≤1000,a≤2500`（无兜底，区间外未分级） | 启动即启用 |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/versions?activate=false` | 发布新版本（1~20 条规则），不影响进行中的重判；`activate=true` 发布即启用 |
| GET | `/api/versions` | 版本列表（含是否启用、规则数） |
| GET | `/api/versions/{code}/rules` | 查看某版规则（按判定顺序） |
| POST | `/api/versions/{code}/activate` | 启用版本，返回 `switched`：仅切到另一个版号时为 `true`，重启用现行版为 `false` |
| GET | `/api/active` | 当前启用版号 |
| POST | `/api/rejudge` | 按批重判并入仓最后一次成功结果；期间实际换过版则 409 整批退回 |
| GET | `/api/batches/{batch_no}` | 下游按批号查最后成功结果（含 `judged_version`） |
| GET | `/api/batches/{batch_no}/rejections` | 该批退回记录（无解 / 换版，含重判用版、复核版、换版链路） |
| GET | `/api/rejections` | 全部退回审计 |
| GET | `/health`、`/docs`、`/openapi.json` | 健康检查与交互文档 |

### 在 /docs 演示

1. 打开 `http://localhost:8000/docs`，`POST /api/rejudge` 的请求体已内置示例 `BATCH-DEMO-001`；
2. 直接 Execute 可看到：唯一定级样、多等级（含未分级 `null`）样各自的见证点与可行对数；
3. 调 `POST /api/versions` 发布 `v3` 并 `activate=true`，再重提同一批号 => 结果按新版覆盖；
4. 构造一个水分/灰分都在 9000 以上、原等级却填"优质煤"的样 => 得到 422 及无解样号。

### 响应示例（多等级样）

```json
{
  "sample_no": "S-002",
  "original_grade": "普通煤",
  "unique": false,
  "final_grade": null,
  "options": [
    {"grade": "普通煤", "witness_moisture": 900, "witness_ash": 2300, "feasible_pairs": 20301},
    {"grade": null,      "witness_moisture": 900, "witness_ash": 2501, "feasible_pairs": 80350}
  ]
}
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

- `tests/test_rejudge.py`：闭区间端点、未分级、无解、原版未分级，并在 11×11 小域上做
  **300 组随机规则 × 随机区间的暴力枚举对拍**（等级集合、可行对数、见证点全部核对）；
- `tests/test_service.py`：整批退回、唯一未分级、版本漂移（含 `v2→v3→v2` 切回仍 409、
  只发布不启用/重启用现行版不退回、发布即启用算换版、纪元语义、跨连接守卫、旧库迁移）、
  最后成功结果覆盖与保留、版号冲突、入参校验；
- `tests/test_api.py`：TestClient 端到端覆盖全部 HTTP 状态码、换版 409 报文与下游查询语义。

## 设计要点与取舍

- **版本漂移防护（换版纪元）**：新增 `activation_log` 审计表，每次启用追加一行，
  仅切到另一个版号时 `switched=1` 并推进单调纪元；重判开始记录当前纪元为基线，
  `save_batch_result` 在 `BEGIN IMMEDIATE` 写事务内查询是否存在 `id > 基线` 的
  `switched=1` 行——存在即说明期间实际换过版（哪怕又切回快照版），回滚并退回 409。
  SQLite 单写者语义保证同库多个服务进程遵守同一规则：拿到写锁后看到的纪元是最新值，
  任何在本事务之前提交的换版都不会漏判。
- **复核版号在事务内读取**：409 与退回记录所需的 `review_version` 直接取自入仓
  写事务内读到的 `active_version`，不再在提交失败后另开连接查询，杜绝
  "复核时是 v3、切回 v2 后却把复核版记成 v2" 的错报。
- **旧库平滑升级**：启动时对旧库自动补建 `activation_log`，并写入一条 `switched=0`
  的基线行（不推进纪元）；现行启用版、`batch_results`、`batch_rejections` 原样保留，
  升级前的批次结果与退回记录不受影响。
- **退回可审计**：无解与换版退回都写 `batch_rejections`，但绝不写/改 `batch_results`，
  下游查询语义稳定为"最后一次成功结果"。
- **并发**：SQLite WAL + `busy_timeout=30s`，读不阻塞写、写串行化；每次请求短连接。
- 未分级在 API 中统一以 `grade: null` 表达；多等级选项按等级名排序、未分级固定排末尾，输出确定。
