# R-12 — `/history` 长期趋势查询（小时级下采样，跨天可见）

> **@//codebuddy/** — 本需求由本地（主代理）撰写，指派 codebuddy NPC 读仓执行。
> 本地**只写需求规格、不写业务修复代码**；codebuddy 落地后主代理按两点 diff 白名单
> 核验 + CI 双绿回收。
>
> 状态：**待执行（新需求 / 优化类）**。实证见 §1，非推测。
> ⚠️ 本需求改 `init.sql` + `main.py`，属**中风险**，§5/§6 的 R-03 回归护栏为硬门槛。

---

## 1. 背景（file:line 实证）

`GET /robots/{id}/history`（`backend_ingestion/app/main.py:244-368`）当前只有两条数据源：

- **连续聚合 `robot_history_10s`**（10s 桶，`main.py:252` 起，定义见 `infra/init.sql §5.1`）；
- **原始表 `robot_telemetry` 回退**（仅当聚合空，`main.py:327-331`）。

`minutes` 参数**无上限**（`main.py:278`：`span_s = max(1, minutes) * 60`）。后果：

- 查 `?minutes=1440`（24h）→ 聚合返回 **8640 个 10s 桶/台 × 100 台 ≈ 86 万点**；
- 前端 Drawer（`RobotDetailDrawer.tsx`）按 10s 点渲染必然卡死/空白，运维也无从看「全天趋势」；
- 回退原始表时更重（5 分钟窗口已 ~30 万行，24h 窗口无法承受）。

**真实需求**：运维要看「某机器人过去 24h 的关节温/电量趋势」→ 需要**小时级下采样视图**
（24 点而非 8640 点），既可读又不压垮查询。当前系统**完全没有**这个能力。

> 注：现有 10s 路径对 Drawer 实时折线（5min）是正确且经 R-03 锁定的，本需求**绝不改动它**。

---

## 2. 目标

- 新增**小时级连续聚合** `robot_history_1h`（结构镜像 `robot_history_10s`）。
- `history()` 按 `minutes` 阈值**路由**：小跨度走 10s（不变），大跨度自动走 1h（下采样）。
- 给 `minutes` 加上限保护，杜绝「查 1 年」把库拖垮。
- 响应字段与现有 10s 路径**逐字对齐**（列名即响应字段名，沿用 `main.py:282-298` 的 `_select` 模板）。

---

## 3. 范围边界（明确不做）

- **不改** 10s 路径的任何行为（R-03 §4 契约、R-09 验收全部保持）。
- **不改** `_parse_resolution`（`main.py:371-416`，R-03 §4.1 锁定的整数秒语义保持不变）；
  小时级下采样由 `minutes` 阈值触发，**不引入 `resolution=1h`** 新值（避免回归 R-03）。
- **不新增** 每遥测的额外写库（聚合由 TimescaleDB 刷新策略异步维护，与既有一致）。
- 前端 Drawer 仍用 `?minutes=5&resolution=10s`（不变）；长期趋势由 Grafana/外部消费（见 R-11），
  不在本需求改前端。

---

## 4. 任务分解

### T1 — 新增小时级连续聚合（`infra/init.sql`）
- 镜像 `robot_history_10s` 的定义（`init.sql §5.1`），`time_bucket('1 hour', time)`，
  同组列：`last(status_code,time)` / `last(battery_pct,time)` / `max(max_joint_temp)` /
  `last(joint_temperatures,time)` / `last(joint_torques,time)` /
  **`last(joint_positions,time)`（必须保留，R-03/R-09 踩坑：漏列→Drawer 第三条折线恒空）**。
- 刷新策略：`policy_refresh_continuous_aggregate`，`start_offset` / `end_offset` 合理（参考 10s 的 `end_offset=10s` → 1h 可用 `end_offset=1h` / `schedule=1h`）。
- 保留策略沿用既有（不重复建）。

### T2 — `history()` 按跨度路由（`main.py`）
- 新增阈值常量（如 `HOURILY_AFTER_MINUTES = 180`，即 >3h 走小时桶）；
- 当 `minutes > 阈值`：`_select` 的 `source` 换成 `robot_history_1h`、`bucket` 用 `time_bucket('1 hour', time)`、`time_col=bucket`；
  列集与 10s 路径**逐字一致**（复用同一 `_select` 模板，仅 FROM/WHERE 不同，与 `main.py:282-307` 现有写法一致）。
- `minutes` 上限：超过 `MAX_HISTORY_MINUTES`（建议 7d = 10080）→ `raise HTTPException(400, "history span too large")`（在 `_parse_resolution` 之外、于 `minutes` 校验处加）。
- 响应 `resolution` 字段回显实际桶秒数（10s 路径回 `10s`/等；小时路径回 `3600s`），与 `main.py:355` 一致。

### T3 — 回退一致性
- 小时路径同样保留「聚合空则回退原始表」语义（`main.py:326-331` 逻辑复用），避免冷启动空白。

---

## 5. 验收标准（可判定）

| # | 用例 | 期望 | 判定依据 |
|---|---|---|---|
| V1 | 聚合存在 | `robot_history_1h` 物化视图在 `init.sql` 创建成功，刷新策略注册 | `init.sql` 执行 + `\dv` |
| V2 | 小跨度不变 | `?minutes=5&resolution=10s` 行为、返回列、点数与改动前**逐字一致**（R-03 回归） | 对照 `main.py:282-368` 原逻辑 + 单测 |
| V3 | 大跨度下采样 | `?minutes=1440` 返回约 **24 点/台**（1h 桶），非 8640 点 | 真实库灌数后 `count(*)` |
| V4 | 列完整 | 小时路径响应含 `joint_positions`（Drawer 第三折线不空） | `jsonb_typeof` / 前端对照 |
| V5 | 上限保护 | `?minutes=20000` → HTTP 400 `history span too large` | curl |
| V6 | 延迟下降 | 大跨度查询 p95 显著低于直查原始表（真快，非观测假快） | `history_latency_seconds` 对比 |
| V7 | 无新写库 | 聚合由刷新策略异步维护，落库批次数 / `telemetry_written_total` 不因本需求变化 | `/healthz` 计数对照 |

---

## 6. 约束 / 禁止项

- 禁止改动 10s 路径（`main.py:300-343`）任何逻辑 —— R-03 §4 契约硬红线。
- 禁止改动 `_parse_resolution`（R-03 §4.1 整数秒语义）。
- 禁止在 `robot_history_1h` 漏 `joint_positions` 列（R-03/R-09 已踩坑）。
- 禁止为通过而放宽 `minutes` 上限（防拖垮查询）。
- 禁止引入每遥测额外 DB 写（聚合异步刷新，与既有一致）。
- `git diff` 白名单：`infra/init.sql` + `backend_ingestion/app/main.py:history`；不得夹带其它模块。

---

## 7. 交付物（codebuddy 产出，主代理回收核验）

- `infra/init.sql`：`robot_history_1h` 连续聚合 + 刷新策略。
- `backend_ingestion/app/main.py`：`history()` 跨度路由 + `minutes` 上限。
- 单测：覆盖 V2（10s 不变）/ V3（1h 下采样）/ V5（上限 400）。
- PR 的 `git diff` 白名单仅 `init.sql` + `main.py:history` 相关，CI 双绿（test + e2e 30k 实跑仍 ≥30000 行）。
