# R-16 — `robot_status_10s` 实时聚合回填（看板最新态 1min 空洞，R-13 同类 sibling）

> **@//codebuddy/** — 本需求由本地（主代理）撰写，指派 codebuddy NPC 读仓执行。
> 本地**只写需求规格、不写业务修复代码**；codebuddy 落地后主代理按两点 diff 白名单核验 + CI 双绿回收。
>
> 状态：**待执行（修复类，中风险）**。实证见 §1，非推测。
> ⚠️ 与 R-13 同机制（cagg `materialized_only`），但**只修 `robot_status_10s`**，不得回退 R-13 已修状态。

---

## 1. 背景（file:line 实证）

### 1.1 根因（R-13 真库实测已确立）
TimescaleDB **2.13.1** 连续聚合默认 `materialized_only = true`（源码 `src/ts_catalog/continuous_agg.c:58-62` 的 `default_val = (Datum)true`）。
默认 true ⇒ cagg 查询视图是**纯物化表**（`pg_get_viewdef` 无 `UNION ALL … FROM robot_telemetry` 实时段），`[now - end_offset, now]` 这段**查不到**。

### 1.2 R-13 只修了 `/history` 两个，本视图未动
- R-13 修复落在 `infra/init.sql:307-308`：**仅** `robot_history_10s` 与 `robot_history_1h` 两条 `ALTER … SET (timescaledb.materialized_only = false)`；
- `robot_status_10s` 定义在 `:122`、刷新策略在 `:135-139`（`end_offset => INTERVAL '1 minute'`、`schedule_interval => INTERVAL '1 minute'`），**未被 R-13 触碰**；
- `init.sql:251` 与 `:300` 的注释已显式标注：「`robot_status_10s`（看板最新态）为同类 sibling 缺口，按 R-13 §3 **本轮不动**，另开需求评估」。

### 1.3 后果（真，非推测）
`robot_status_10s` 服务**「看板最新态」**（`:148` 注释：本视图服务看板最新态）。因其仍是 materialized-only 且 `end_offset=1min`：
⇒ 看板"每台机器人当前状态/电量/温度"**缺失最近 1 分钟**的最新数据，最新值最多滞后约 1 分钟（且受 `schedule=1min` 与 `end_offset=1min` 叠加影响，最坏更久）。
这与 R-13 修掉的 `/history` 空洞**同根同源**，只是消费方与 `end_offset` 不同。

---

## 2. 目标

- 对 **`robot_status_10s`** 显式 `ALTER MATERIALIZED VIEW … SET (timescaledb.materialized_only = false)`，开启实时聚合，使看板最新态**不再有 1min 空洞**（右侧实时段由引擎对 `robot_telemetry` 透明回填）；
- 加**真 TimescaleDB 回归测试**，把"实时回填生效"锁成可失败信号（一旦被改回 true 立即红）；
- **不动**刷新策略、列集、前端。

---

## 3. 范围边界（明确不做）

- **只改 `robot_status_10s` 的属性**（`materialized_only=false`）；
- **不改** 其刷新策略（`init.sql:135-139` 的 `start_offset`/`end_offset=1min`/`schedule_interval=1min` **保持原值**）；
- **不改** 列集（`status_code`/`battery_pct`/`max_joint_temp`/`error_code`，`:127-130`）；
- **不改** 前端（`RobotCard`/看板）与 `backend_ingestion` 的查询；
- **不得回退** R-13 已修状态（`:307-308` 两条 ALTER 必须原样保留）；
- **不修** `robot_history_10s`/`robot_history_1h`（R-13 已修，本需求不得改动它们）。

---

## 4. 任务分解

### T1 — 注释（`infra/init.sql`）
在 `robot_status_10s` 定义块（`:122`）上方补注释，说明：
- 2.13.1 连续聚合默认 `materialized_only=true`（源码 `continuous_agg.c:58-62`），本视图建完即 true；
- 后果：看板最新态缺最近 1 分钟实时段；
- 本需求用 `ALTER … SET (timescaledb.materialized_only = false)` 修复（幂等，re-run `init.sql` 安全）；
- **禁止**改回 `true`；`end_offset=1min` 只界定物化边界，右侧由引擎透明回填。

### T2 — 修复（`infra/init.sql`，DDL，幂等）
在 R-13 的 §5.1c（`init.sql:307-308`）之后**追加一条**（顺序无依赖，幂等即可）：
```sql
-- R-16：`robot_status_10s`（看板最新态）同为 materialized-only，看板最新值滞后约 1min。
ALTER MATERIALIZED VIEW IF EXISTS robot_status_10s SET (timescaledb.materialized_only = false);
```
（`ALTER … SET (…=false)` 对已是该值的视图重复执行是 no-op；`IF EXISTS` 兜底重跑安全。不得删除/修改 `:307-308` 的两条。）

### T3 — 真 TimescaleDB 回归测试
- 复用 R-13 建立的真库测试模式（`tests/e2e/test_cagg_realtime_backfill.py`，其断言函数 `assert_cagg_realtime_backfill` 已可用）；
- 新增/扩展用例覆盖 **`robot_status_10s`**：
  1. 断言 `_timescaledb_catalog.continuous_agg` 中 `robot_status_10s.materialized_only = false`；
  2. 断言 `pg_get_viewdef('robot_status_10s')` 含 `UNION ALL … FROM robot_telemetry` 实时段；
  3. 灌 `[now-30s, now]`（落在 `end_offset=1min` 的实时区内）→ 断言 `SELECT count(*) FROM robot_status_10s WHERE bucket >= now()-'90 seconds' AND robot_id=$1` **> 0**。
- **禁止用 mock pool 伪造**（ISSUE #60 禁止 mock 替身冒充真实 TimescaleDB）；复用 `tests/e2e/docker-compose.e2e.yml` 的 `timescaledb:2.13.1`。
- 若并入 `test_e2e_30k.py`（栈还活着时调用），须沿用 R-13 的既有接入方式，**不得**破坏 30k landing 断言。

---

## 5. 验收标准（可判定）

| # | 用例 | 期望 | 判定依据 |
|---|---|---|---|
| V1 | 注释准确 | `init.sql:122` 上方含「2.13.1 默认 true / 本修复改 false / 禁 true」说明 | 读 `init.sql` |
| V2 | 真库修复生效 | 真 timescaledb 2.13.1 上，`robot_status_10s`：`materialized_only=false`、viewdef 含实时段、灌 `[now-30s,now]` 后 `count>0` | 真库测试输出 |
| V3 | R-13 未回退 | `init.sql:307-308` 两条 `robot_history_10s`/`robot_history_1h` 的 `SET(false)` **原样保留**；其测试仍绿 | diff + grep |
| V4 | 刷新策略未动 | `robot_status_10s` 的 `end_offset=1min`/`schedule=1min`/`start_offset` 保持 `init.sql:135-139` 原值 | diff |
| V5 | 列与前端未动 | 列集不变；前端/`backend_ingestion` 零改动 | diff |
| V6 | CI 双绿 | `pipeline-1(test)` + `pipeline-2(e2e-30k-landing)` 均 success | `cnb pulls list-pull-commit-statuses` |

---

## 6. 约束 / 禁止项（白名单）

- `git diff` 白名单：`infra/init.sql`（`robot_status_10s` 注释 + 1× `ALTER … SET(false)`）+ 真库回归测试文件。
- 禁止改动/删除 R-13 的 `:307-308` 两条 ALTER（回退即回归）。
- 禁止改 `robot_status_10s` 的刷新策略（`init.sql:135-139`）。
- 禁止改列集、前端、`backend_ingestion` 查询。
- 禁止用 mock pool 伪造实时回填（ISSUE #60）。
- 不得夹带其它模块改动。

---

## 7. 交付物

- `infra/init.sql`：`robot_status_10s` 块上方注释 + 1× `ALTER … SET (timescaledb.materialized_only = false)`（幂等）。
- 真 TimescaleDB 回归测试：`robot_status_10s` 实时回填三断言。
- PR 的 `git diff` 白名单如上；CI 双绿。

## 8. 关联

- 来源：R-13 交付时 codebuddy 主动提示的**同类 sibling 缺口**（`init.sql:251`/`:300` 已标注"另开需求评估"）。
- 同根：`docs/GAP-SCAN-2026-10-13.md` GAP-1（已由 R-13 修）；本需求补上未修的第三个 cagg。
