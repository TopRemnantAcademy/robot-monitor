# R-13 — `robot_history_1h`/`robot_history_10s` 实时聚合回填修复 + 真库回归测试（GAP-1 **复核为真 bug**）

> **@//codebuddy/** — 本需求由本地（主代理）撰写，指派 codebuddy NPC 读仓执行。
> 本地**只写需求规格、不写业务修复代码**；codebuddy 落地后主代理按两点 diff 白名单
> 核验 + CI 双绿回收。
>
> 状态：**待执行（修复类，中风险）**。实证见 §1，非推测。
> ⚠️ 本需求**改 `init.sql` 的 cagg 属性**（非刷新策略/列/查询），§6 红线为硬门槛。
> ⚠️ **更正（2026-10-13 续）**：初版（commit `7ed6cba`）误判 GAP-1 为 false positive（基于未核实的
> 「TimescaleDB 2.x 默认 materialized_only=false」假设）。codebuddy 在**真** timescaledb 2.13.1 实测后
> 驳回：默认实为 **true**（源码 + 上游测试双证据）。故 GAP-1 是**真 bug**，本需求改为**修复版**（见 §1.2）。

---

## 1. 背景（file:line 实证，含 GAP-1 误判→真 bug 的复核）

### 1.1 原 GAP-1 判定（深度扫描）
`docs/GAP-SCAN-2026-10-13.md` GAP-1 判定：长跨度 `/history` 走 `robot_history_1h`（R-12 加，`HOURILY_AFTER_MINUTES=180`，`backend_ingestion/app/main.py:308-309`），其 `end_offset=1h`（`infra/init.sql:254-258`）+ `WITH NO DATA`（`:224,239`）→ 长查询右端 `[now-1h, now]` 空洞。

### 1.2 主代理初版误判（已推翻，诚实记录）
主代理初版假定「TimescaleDB 2.x 连续聚合默认 `materialized_only=false`（实时聚合），故无空洞」→ 把 GAP-1 当 false positive，R-13 初版只做「防回归注释 + 测试」、禁止改 DDL。
**该假定未经真库核实，是错的。**

### 1.3 codebuddy 真库实测（权威证据，推翻 1.2）
codebuddy 装**真** `timescaledb 2.13.1`（与 `infra/docker-compose.yml:45` 同版本）+ 跑 `infra/init.sql`（rc=0），实测：

- **① cagg 目录**：`_timescaledb_catalog.continuous_agg.materialized_only` →
  `robot_history_10s = t`、`robot_history_1h = t`、`robot_status_10s = t`（**三者皆 true**）；
  `timescaledb_information.continuous_aggregates` 同值；
  `pg_get_viewdef('robot_history_1h')` 是**纯物化表** `SELECT … FROM _materialized_hypertable_5`，**无 `UNION ALL` 实时段**（实时聚合的 viewdef 必含 `UNION ALL … FROM robot_telemetry`）。
- **② 按原 T2 原样执行**：`INSERT robot_telemetry` 1800 行、`time ∈ [now-1800s, now]`（即 `[now-30min, now]`）；
  `SELECT count(*) FROM robot_history_1h WHERE bucket >= now()-'90 min' AND robot_id='humanoid-001'` → **0**（实时段未回填）；
  底层 `robot_telemetry` 同条件 = 1800（数据在，只是没进 cagg）。
- **③ 对照实验**：`ALTER MATERIALIZED VIEW robot_history_1h SET (timescaledb.materialized_only=false);` → 同一查询立刻 **1**。
  ⇒ 「0 / 1」差异完全由 `materialized_only` 决定。
- **④ 根因（上游源码 + 上游测试，双证据）**：
  - `timescaledb 2.13.1` 源码 `src/ts_catalog/continuous_agg.c:58-62`：
    `[ContinuousViewOptionMaterializedOnly] = { .arg_name = "materialized_only", .default_val = (Datum)true };`
    **默认值是 `true`**；`WITH NO DATA` 时无人翻转它，仅显式 `materialized_only=false` 才翻转。
  - 上游回归测试 `tsl/test/expected/cagg_union_view-15.out`：
    `WITH (..., materialized_only=false)` → `materialized_only = f` + **UNION 实时视图**；
    `ALTER … SET (materialized_only=true)` → `materialized_only = t` + 非 union 视图、`SELECT` 返 `(0 rows)`。

⇒ **GAP-1 是真 bug**：`robot_history_1h`（长跨度）+ `robot_history_10s`（短跨度，end_offset=10s）都因 `materialized_only=true` 缺失右端实时段；`/history` 两条路径右端均有空洞（1h / 10s）。初版 R-13 的「防回归注释」不足以修复，必须显式 `SET (materialized_only=false)`。

---

## 2. 目标

- **修复**：对 `/history` 的两个数据源 cagg（`robot_history_1h` + `robot_history_10s`）显式 `SET (timescaledb.materialized_only=false)`，
  开启实时聚合，使查询透明回填 `[now-end_offset, now]`，消除 `/history` 右端空洞。
- **回归测试（真 TimescaleDB）**：灌 `[now-30min, now]` 数据，断言 `robot_history_1h` 与 `robot_history_10s` 均能查回该段（>0），
  把「实时回填生效」锁成可失败信号（一旦被改回 true，测试立即红）。
- **注释（准确）**：`init.sql` 标注 2.13.1 默认 `materialized_only=true` 的 footgun + 本修复的作用。

---

## 3. 范围边界（明确不做）

- **修 `/history` 两个 cagg**：`robot_history_1h` + `robot_history_10s`（同缺陷、同 1 行修复，避免 `/history` 半修）。
- **不修 `robot_status_10s`**（看板「最新态」，end_offset=1min → 1min 空洞）：属**同类 sibling 缺口**，影响前端实时看板最新值，
  不在 R-13 范围；列为**后续单独评估**（避免未授权改动看板最新态语义）。如需一并修，另开需求。
- **不改** 刷新策略（`init.sql:198-202` / `:254-258` 的 `start/end_offset`/`schedule` 保持）。
- **不改** cagg 列集 / `_select` 模板 / `history()` 路由 / `_parse_resolution`（R-03 §4 / §4.1 硬红线不变；实时回填透明，列与 SQL 字节级不变）。
- **禁止** 设 `materialized_only=true`（那正是 bug 本身）。
- 不在 mock pool 单测里伪造实时回填（ISSUE #60 禁止 mock 替身冒充真实 TimescaleDB）。

---

## 4. 任务分解

### T1 — 准确注释（`infra/init.sql`，仅注释，置于 cagg 定义块上方）
在 `robot_history_1h`（`:224`）/ `robot_history_10s`（`:168`）定义块上方补注释：
- TimescaleDB `2.13.1` 连续聚合**默认 `materialized_only=true`**（源码 `continuous_agg.c:58-62`），非实时聚合；
- 故 cagg 不回填 `[now-end_offset, now]`，`/history` 右端有空洞（1h / 10s）；
- 本需求用 `ALTER … SET (timescaledb.materialized_only=false)` 改实时聚合（见 T2）；**禁止**改回 true；
- `end_offset` 仅界定物化边界，实时段由引擎对 `robot_telemetry` 透明回填。

### T2 — 修复 cagg 属性（`infra/init.sql`，DDL 变更，幂等）
在 cagg 创建之后追加（re-run `init.sql` 安全，幂等）：
```sql
-- R-13 修复：2.13.1 默认 materialized_only=true → /history 右端空洞。改实时聚合。
ALTER MATERIALIZED VIEW IF EXISTS robot_history_1h  SET (timescaledb.materialized_only=false);
ALTER MATERIALIZED VIEW IF EXISTS robot_history_10s SET (timescaledb.materialized_only=false);
```
（`ALTER … SET (…=false)` 重复执行是 no-op，不报错；`IF EXISTS` 兜底重跑安全。）

### T3 — 真 TimescaleDB 回归测试（证明实时回填生效）
- 位置：复用 `tests/e2e/docker-compose.e2e.yml` 的 `timescaledb:2.13.1`（ISSUE #60 禁 mock）。
- 步骤：跑 `infra/init.sql`（含 T2 修复）→ 向 `robot_telemetry` 插 `[now-30min, now]` 数据 →
  断言 `SELECT count(*) FROM robot_history_1h WHERE bucket >= now()-90min AND robot_id=$1` **>0**
  **且** `SELECT count(*) FROM robot_history_10s WHERE bucket >= now()-90min AND robot_id=$1` **>0**。
- 反向不变量（推荐）：若把某 cagg 改回 `materialized_only=true`，对应断言应红 → 本测试即该回归警报。
- 标记真库测试，禁 mock pool 替代。

---

## 5. 验收标准（可判定）

| # | 用例 | 期望 | 判定依据 |
|---|---|---|---|
| V1 | 注释准确 | `init.sql` 的 `robot_history_1h`/`robot_history_10s` 块上方含「2.13.1 默认 materialized_only=true / 本修复改 false / 禁 true」 | 读 `init.sql` |
| V2 | 真库修复生效 | 真 timescaledb 2.13.1 上，`robot_history_1h` 与 `robot_history_10s` 查 `[now-90min,now]` 均返 `>0`（灌 `[now-30min,now]` 后） | 真库测试输出 |
| V3 | 显式 false | `init.sql` 含 `SET (timescaledb.materialized_only=false)` 且无 `=true` | `grep materialized_only init.sql` |
| V4 | 既有契约不动 | `test_history_source.py` 原断言（10s 路由 / 1h 路由 / 上限 400 等）仍 PASS；列集/`_select` 字节级不变 | 单测 + CI |
| V5 | CI 双绿 | `pipeline-1(test)` + `pipeline-2(e2e-30k-landing)` 均 success（e2e 真绿：rows_written≥30000 且无 `【E2E SKIPPED】`） | `cnb pulls list-pull-commit-statuses` + 按需 `get-build-logs` |

---

## 6. 约束 / 禁止项（白名单）

- `git diff` 白名单：`infra/init.sql`（准确注释 + 2× `ALTER … SET (materialized_only=false)`，幂等）+ 新增真库测试文件。
- 禁止设 `materialized_only=true`（即 bug 本身）。
- 禁止改刷新策略（`init.sql:198-202`/`:254-258`）、cagg 列集、`_select` 模板、`history()` 路由、`_parse_resolution`（R-03 §4/§4.1）。
- 禁止改 `robot_status_10s`（看板最新态，sibling，留后续评估）。
- 禁止在 mock pool 单测里伪造实时回填（ISSUE #60）。
- 不得夹带其它模块改动（metrics/prometheus/dashboards/前端等）。

---

## 7. 交付物（codebuddy 产出，主代理回收核验）

- `infra/init.sql`：`robot_history_1h`/`robot_history_10s` 块上方准确注释 + 2× `ALTER … SET (timescaledb.materialized_only=false)`（幂等）。
- 真 TimescaleDB 回归测试（如 `tests/e2e/test_cagg_realtime_backfill.py` 或并入现有 e2e）：灌 `[now-30min,now]` → 断言两 cagg 均查回。
- PR 的 `git diff` 白名单仅 `init.sql`（注释 + 2 ALTER）+ 测试文件；CI 双绿（test + e2e-30k-landing 真绿）。

---

## 8. 关联

- 来源：`docs/GAP-SCAN-2026-10-13.md` GAP-1（**复核为真 bug**，非误判）。
- 根因：`timescaledb 2.13.1` 连续聚合默认 `materialized_only=true`（源码 `continuous_agg.c:58-62`）。
- 上层契约：R-12（`/history` 长期趋势）+ R-03 §4（`/history` 响应契约，列/SQL 不变即守住）。
- 后续：`robot_status_10s` 同类 sibling（看板最新态 1min 空洞）→ 单独评估。
