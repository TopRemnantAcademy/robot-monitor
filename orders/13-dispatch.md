# 订单 13 — 收尾整合：可观测性接线 + 6 个分支的冲突收敛

> 基线：`main@7061714af5e8b4005027c81d88736c7dc4a5c78e`
> 派单时间：2026-10-11
> 派单方：主代理（本机已实测，以下所有"实测"结论均可直接采信，无需重复调研）

---

## 0. 背景：我已经做完了什么，你还需要做什么

主代理已在本机完成**第一阶段整合**并推送到 `main@7061714`。
**新增文件**已全部回收，**依赖**已落地，**两处纠错**已应用。

**你的任务只有一件事：把剩余 6 个分支的「修改型改动」收敛到 `main@7061714` 上，
并把已回收但未接线的 `metrics` 模块真正挂到服务里。**

### 已回收（不要重复做，也不要覆盖）

| 内容 | 来源分支 |
|---|---|
| `backend_ingestion/tests/{README.md,verify_batch_writer.py}` | `fix/issue19-merge-safe` |
| `backend_ingestion/app/metrics.py`、`alert_engine/app/metrics.py` | `codebuddy/observability-direct-wiring` |
| `alert_engine/Dockerfile`、`infra/docker-compose.app.yml`、`infra/observability/**` | 同上 |
| `backend_ingestion/app/db/pool.py`、`alert_engine/app/pool.py` | `feat/order-08-asyncpg-upgrade` |
| `asyncpg 0.29.0 → 0.32.0`、`prometheus-client==0.21.1` | 主代理手工应用 |

### 已应用的两处纠错（不要回退）

1. **`backend_ingestion/app/db/models.py` 的 `RobotAlert`**：已改为
   列上 `index=True` + `__mapper_args__ = {"primary_key": [time, robot_id]}`。
   实测：`configure_mappers()` 通过，且 `CreateTable(...).compile(postgresql)`
   产出的 DDL **不含 PRIMARY KEY**，与 `infra/init.sql` 的 hypertable 一致。
2. **`external/ORDER12-VERIFICATION.md` §2**：推翻了此前
   "aiomqtt 2.0.1 `__aexit__` 吞异常导致重连不执行" 的错误结论。
   实测 v2.0.1→v2.5.0 源码 diff：2.0.1 是 `raise exc`，2.5.0 **删掉了**它。

---

## 1. 任务 A（最高优先）：把 `metrics` 真正接线

### 现状（实测）

```
backend_ingestion/app/main.py : grep metrics → 空（未接线）
alert_engine/app/main.py      : grep metrics → 空（未接线）
backend_ingestion/app/metrics.py:53 : def render() -> tuple[bytes, str]   # 已存在
```

模块存在、依赖已装（`prometheus-client==0.21.1`），但**没有任何路由暴露它**。
现在的可观测性是"装了仪表盘但没接电线"。

### 要求

1. 在**两个服务**的 `main.py` 各挂一个 `GET /metrics`，
   返回 `metrics.render()` 的 `(body, content_type)`，
   用 `fastapi.responses.Response(content=body, media_type=ctype)`。
2. **函数名禁止叫 `metrics`** —— 会与 `from app import metrics` 的模块名互相遮蔽。
   `fix/issue19-merge-safe` 正是踩了这个坑（它 `@app.get("/metrics")` 下面
   `async def metrics():`），本订单要的是修正后的写法，函数名用 `prometheus_metrics`。
3. **只挂一次**，两个服务各自一份，不要在同一服务里出现两条 `/metrics`。
4. 暴露前刷新瞬时值：`metrics.queue_depth.set(writer.queue.qsize())` 之类，
   具体指标以 `app/metrics.py` 里已定义的为准，不要新增指标。

---

## 2. 任务 B：收敛 6 个分支的修改型改动

### ⛔ 硬性禁止（这些文件一律保留 `main@7061714` 的版本，理由已实测）

| 文件 | 禁止原因（实测结论） |
|---|---|
| `backend_ingestion/app/services/batch_writer.py` | `fix/issue19-merge-safe` 的版本**会删掉整条三级降级链**（L1 COPY→临时表→`INSERT ... SELECT ... ON CONFLICT DO NOTHING`、L2 `executemany`、L3 二分隔离坏行）。这是 `spec/01 §4.2` 明确要求的容错核心，由 `auto/db-integration-copy` 引入并已合入 `main`。`feat/order-08` 的版本则**缺 `_STOP` 哨兵**，会让 `graceful_shutdown()` 唤不醒阻塞在 `queue.get()` 的 `run()`。两个版本都不可取。 |
| `backend_ingestion/app/db/models.py` | `fix/infra-image-facts-and-contract-drift` 的版本用 `sqlalchemy.JSON`（会编译成 `JSON` 而非 `JSONB`，回退类型漂移修复）且 `Mapped[str]` 标注 TIMESTAMP。主代理已应用正确版本。 |
| `requirements.txt`（两个服务） | `feat/order-08` 的版本把 **aiomqtt 从 2.5.1 退到 2.0.1**（其基线早于订单 07）。`aiomqtt==2.5.1` 与 `asyncpg==0.32.0` 已就位，保持不动。 |
| `test_integration.sh` | 保留 `main` 版（293 行，含超时守卫框架；对方 230 行是其真子集）。 |
| `frontend_dashboard/src/lib/echartsCore.ts` 及前端 bundle 相关 | 订单 09 成果（1209→671 kB），禁止回退。 |

### ✅ 需要你收敛进去的改动

#### B1. 来自 `fix/issue19-merge-safe`（`bffc6ca`）

- **`backend_ingestion/app/main.py`**：只取 `/healthz` 里新增的
  **丢行三元组**字段（ISSUE #19 G2）：
  `rows_submitted` / `rows_written` / `rows_skipped`，
  其中 `skipped` 是 `ON CONFLICT DO NOTHING` 静默丢弃的行数，
  即"收到的数据没全部入库"的直接证据。
  ⚠️ **必须保留 `main` 现有的 `/healthz` 语义**：`status` 反映依赖可达性
  （`ok` / `degraded`），并保留 `db`、`retries`、`rejected` 字段与那段
  解释"为何返回 200 而非 503"的注释。该分支把这些全删成硬编码
  `"status": "ok"`，是**回退**，不得采纳。
  注意：`writer` 上若还没有这些计数器，在 `batch_writer.py` 里**增量添加**，
  不得改动既有的降级链逻辑。
- **`infra/init.sql`**：查看其改动，若与 `main` 的 hypertable/JSONB 定义不冲突则合入，
  冲突则保留 `main` 并在交付说明里写明取舍。
- **`.gitignore`**：确保 `.venv-ci` 被忽略（该分支修过误提交的 venv，
  主代理未取这个文件，需要你补上）。

#### B2. 来自 `codebuddy/observability-direct-wiring`（`145e20c`）

- **`alert_engine/app/config.py`、`alert_engine/app/main.py`**：
  收敛其可观测性相关改动（与任务 A 的 `/metrics` 接线一并完成）。
- **`backend_ingestion/app/services/mqtt_subscriber.py`**：
  若其改动与 `main` 上已纠正的注释（指向 `ORDER12-VERIFICATION.md §2`）冲突，
  **保留 `main` 版**，并在交付说明指出。
- **`external/INTEGRATION-PLAN.md`、`external/SOURCES.md`**：见 B4。

#### B3. 来自 `feat/order-08-asyncpg-upgrade`（`b50515d`）

- **`backend_ingestion/app/db/session.py`、`alert_engine/app/db.py`**：
  改为调用已回收的 `app/db/pool.py` / `app/pool.py` 工厂，
  统一 `init_size=min_size, min_size=min_size` 语义。
  ⚠️ 该分支基线早于订单 07，其 `batch_writer.py`、`requirements.txt`
  一律不取（见上面禁止表）。
- **`backend_ingestion/app/config.py`、`backend_ingestion/README.md`、
  `spec/01_backend_data_ingestion_and_storage.md`、
  `docs/requirements/R-07-integration-and-stress.md`、`.env.example`**：
  文档与配置类改动，与 `main` 无实质冲突则合入。

#### B4. 三个纯文档分支 —— 用「并集」策略合并

`cb/issue19-github-rescan`（`9f21cf9`）、`docs/detail-gap-reassessment`（`99c273e`）、
`feat/enterprise-gap-analysis-issue19`（`7f6889b`）三方同时追加了
`STATUS.md`、`external/INTEGRATION-PLAN.md`、`external/SOURCES.md`、
`orders/11-dispatch.md`、`orders/12-dispatch.md`。

规则：**按语义并集保留，不得整文件覆盖**。谁的内容都不许丢；
同一事实有冲突时，保留**附有实证来源**的那一条，
并在交付说明里列出"哪几条事实被判定冲突、为何取这一条"。

---

## 3. 验收标准（必须贴真实输出，禁止描述性声称）

在隔离环境里**实际执行**以下命令并贴出结果：

```bash
python -c "import app.main, app.metrics, app.db.pool; print('backend import OK')"   # 在 backend_ingestion/
python -c "import app.main, app.metrics, app.pool;    print('alert import OK')"      # 在 alert_engine/
python -m pytest alert_engine/tests/ -q                                              # 期望 ≥ 11 passed
```

以及 **HTTP 层实证**（不能只靠 import 通过就宣称完成）：

```bash
uvicorn app.main:app --port 8001 &
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" http://127.0.0.1:8001/metrics
curl -s http://127.0.0.1:8001/healthz
```

- `/metrics` 必须返回 **200** 且 `content_type` 含 `text/plain`（Prometheus  exposition format）。
- `/healthz` 必须仍含 `db`、`retries`、`rejected` 与新增的 `rows_*` 字段。
- `alert_engine` 同样起一次，确认两条 `/metrics` 都通。

另外必须贴出**回归自检**（逐条回答，答"否"要说明理由）：

1. `grep -c "_STOP" backend_ingestion/app/services/batch_writer.py` ≥ 1？
2. `grep -c "L1\|copy_records_to_table" backend_ingestion/app/services/batch_writer.py` ≥ 1（降级链仍在）？
3. `grep "aiomqtt==" backend_ingestion/requirements.txt` 仍是 2.5.1？
4. `grep "asyncpg==" backend_ingestion/requirements.txt` 仍是 0.32.0？
5. `RobotAlert` 的 DDL 仍**不含** PRIMARY KEY？
6. 前端 `frontend_dashboard/src/lib/echartsCore.ts` 仍存在？

---

## 4. 交付方式

- 新建分支 `feat/order-13-observability-wiring`，基于 `main@7061714`。
- 推送该分支即可，**不要直接 push 到 `main`**，主代理负责回收与最终验证。
- 完成后在 Issue 评论里贴：①上述命令的真实输出 ②6 条回归自检的逐条结果
  ③冲突取舍清单 ④未解决项。

## 5. 失败上报

- 若容器网络拉不到 github.com / PyPI，如实报告，不要假装成功。
- 装不上包可用镜像源：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple <pkg>`
  （实测 PyPI 直连 403 可用此镜像绕过）。
- 若发现**本派单中的结论有误**，直接指出并附证据 —— 不要附和。
  主代理此前已有 2 条结论被上游源码推翻并已纠正，纠错是被鼓励的。
