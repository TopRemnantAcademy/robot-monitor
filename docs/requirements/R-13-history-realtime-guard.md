# R-13 — `robot_history_1h` 实时聚合回填保证 + 真库回归测试（GAP-1 复核：原「1h 空洞」为误判）

> **@//codebuddy/** — 本需求由本地（主代理）撰写，指派 codebuddy NPC 读仓执行。
> 本地**只写需求规格、不写业务修复代码**；codebuddy 落地后主代理按两点 diff 白名单
> 核验 + CI 双绿回收。
>
> 状态：**待执行（防回归 / 文档化类）**。实证见 §1，非推测。
> ⚠️ 本需求**不修洞**（洞不存在）；只做「锁定实时聚合保证 + 回归测试 + 文档化 footgun」，
> 属**低风险**（仅增测试 + 注释，不动查询/刷新语义）。

---

## 1. 背景（file:line 实证，含 GAP-1 误判复核）

### 1.1 原 GAP-1 判定与推翻
深度缺口扫描（`docs/GAP-SCAN-2026-10-13.md` GAP-1）曾判定：长跨度 `/history` 走 `robot_history_1h`，
其 `end_offset=1h` + `WITH NO DATA` → materialized-only → 窗口 `[now-1h, now]` **整段空白**。

**复核结论：该判定为误判（false positive）。** 证据链（全部 file:line 实证）：

- `robot_history_1h` 定义为 `CREATE MATERIALIZED VIEW … WITH (timescaledb.continuous) … WITH NO DATA`
  （`infra/init.sql:224-239`）；
- 刷新策略 `add_continuous_aggregate_policy('robot_history_1h', end_offset => INTERVAL '1 hour', …)`
  （`infra/init.sql:254-258`）；
- **全仓库 grep `materialized_only` 0 命中**，且无 `ALTER MATERIALIZED VIEW … SET (materialized_only=true)`
  → 该 cagg 的 `materialized_only` 取 TimescaleDB 2.x 默认值 **`false`**；
- 库版本 `timescale/timescaledb:2.13.1-pg16`（`infra/docker-compose.yml:45`、
  `infra/ops/grafana/provisioning/datasources/datasources.yaml:31`、
  `tests/e2e/docker-compose.e2e.yml:42`）；
- **TimescaleDB 2.x 连续聚合默认为实时聚合（real-time aggregate）**：查询 cagg 视图时，
  引擎自动 `UNION`【已物化区 `>= now-3d AND < now-1h`】+【对底层 `robot_telemetry` 的实时查询 `[now-1h, now]`】，
  **透明回填右端 1h**，无需 `WITH DATA` 或任何查询层改造；
- `history()` 长跨度查询即
  `SELECT … FROM robot_history_1h WHERE robot_id=$2 AND bucket >= now() - ($3||' seconds')::interval …`
  （`backend_ingestion/app/main.py:328-336`），无屏蔽实时段的写法。

⇒ **长跨度 `/history` 当前无右端 1h 空洞**。原 GAP-1 推荐的「`history()` UNION `robot_history_1h` + `robot_history_10s` 补最新生效 1h」修复**不必要**，落地反而引入冗余/重复点，故**禁止**（见 §6）。

### 1.2 为什么仍需本需求（真正的可防护缺口）
当前正确性**仅依赖** `materialized_only` 始终为 `false`。这是一个**隐性 footgun**：

- 未来某次「优化 / 清理」可能误加 `materialized_only=true`（或 `WITH DATA` 后改建为 materialized-only）；
- 一旦如此，`robot_history_1h` 不再实时回填 `[now-1h, now]`，且 `end_offset=1h` 的刷新策略**永远不覆盖**
  该实时段 → 长跨度 `/history` 右端 1h 瞬间变空白（**这正是原 GAP-1 描述的故障**）；
- 该回归**无单测覆盖**（既有 `test_history_source.py` 用 mock pool，验证的是 SQL 拼装，非 DB 引擎实时回填行为）。

故本需求的真实价值 = **把「实时聚合回填」这一隐性不变量变成可验证、可文档化的显式保证**，
防未来回归。属防御性 hardening，非补当前 bug。

---

## 2. 目标

- 在 `infra/init.sql` 的 `robot_history_1h` 定义处**显式文档化**：实时聚合（`materialized_only=false`）是当前正确行为；
  禁止设 `materialized_only=true`（否则长查询右端 1h 空洞）；`end_offset=1h` 仅界定物化边界，实时段由底层 hypertable 透明回填。
- 新增**真 TimescaleDB 回归测试**：灌入落在 `[now-1h, now]` 实时区的数据，断言 `robot_history_1h` 能查回该段，
  把「实时回填生效」锁成可失败的信号（一旦被改成 materialized-only，测试立即红）。
- 不改动任何查询 / 刷新语义，不引入 UNION 补洞。

---

## 3. 范围边界（明确不做）

- **不引入** `history()` UNION `robot_history_1h` + `robot_history_10s` 补洞（GAP-1 误判修复，冗余且有害）。
- **不改** `robot_history_1h` 的刷新策略（`end_offset` / `schedule` / `start_offset` 保持 `init.sql:254-258`）。
- **不设** `materialized_only=true`（禁止项，见 §6）。
- **不改** 10s 路径、`_parse_resolution`、R-03/R-09 契约（与 R-12 同护栏）。
- 不在 mock pool 单测里「伪造」实时回填（ISSUE #60 禁止 mock 替身冒充真实 TimescaleDB；本测试须跑真库）。

---

## 4. 任务分解

### T1 — 文档化 footgun（`infra/init.sql`，仅注释，不改 DDL）
在 `CREATE MATERIALIZED VIEW IF NOT EXISTS robot_history_1h …`（`:224`）定义块上方新增注释块，明确：
- 本视图**依赖实时聚合**（`materialized_only=false`，TimescaleDB 2.x 默认）；
- `end_offset => 1 hour`（`:256`）只界定**物化**边界；`[now-1h, now]` 由引擎对 `robot_telemetry` 的实时查询透明回填；
- **禁止** `SET (materialized_only=true)` 或改建为 materialized-only —— 否则长跨度 `/history` 右端 1h 变空白
  （原 GAP-1 故障模式），且刷新策略永远不覆盖该实时段，无法自愈；
- 回归测试见 T2，改此视图前先确保该测试仍 PASS。

### T2 — 真 TimescaleDB 回归测试（证明实时回填生效）
- 位置：复用既有真库测试入口（`tests/e2e/`，沿用 `tests/e2e/docker-compose.e2e.yml` 的 `timescaledb:2.13.1-pg16`；
  或 `backend_ingestion/tests/` 下接真库 fixtures，与现有 `test_history_source.py` 隔离——后者是 mock 拼装测试，不混用）。
- 步骤：
  1. 跑 `infra/init.sql` 建表 + 建 `robot_history_1h`（确保 cagg 为实时聚合）；
  2. 向 `robot_telemetry` 插入若干行，`time` 落在 `[now-30min, now]`（即实时区，未物化）；
  3. 执行 `SELECT count(*) FROM robot_history_1h WHERE bucket >= now() - INTERVAL '90 minutes' AND robot_id = $1`；
  4. **断言 `count(*) > 0`** —— 证明 `[now-30min, now]` 这段被实时回填进了 cagg 查询（而非等刷新）。
- 反向不变量（可选但推荐）：同样查询在「若有人把 cagg 改成 materialized-only」时应返 0 —— 本测试即可作为该回归的警报。
- 标记：该测试为**真库测试**，禁止用 mock pool 替代（ISSUE #60）；CI 真绿门槛沿用 R-07/R-12 的 `e2e-30k-landing` 同栈。

### T3 — 注释一致性（可选）
若 `robot_history_10s`（`:168`）/`robot_status_10s`（`:122`）亦依赖实时回填，可在其注释补一句「同 `robot_history_1h` 实时聚合约定」，
但不强制；本需求核心是 `robot_history_1h`（长跨度路径）。

---

## 5. 验收标准（可判定）

| # | 用例 | 期望 | 判定依据 |
|---|---|---|---|
| V1 | 注释就位 | `infra/init.sql` 的 `robot_history_1h` 块上方含「实时聚合 / 禁止 materialized_only=true / end_offset 仅界物化」说明 | 读 `init.sql:224` 上方注释 |
| V2 | 实时回填测试 PASS | 灌 `[now-30min, now]` 数据后，`robot_history_1h` 查 `>= now()-90min` 返 `count>0`（真 timescaledb:2.13.1 跑） | 真库测试输出 |
| V3 | 无 materialized-only | 全仓 grep `materialized_only\s*=\s*true` / `SET (materialized_only` 0 命中 | `grep -niE "materialized_only" **/*.sql` |
| V4 | 既有契约不动 | `test_history_source.py` 原断言（V3 指 `?minutes=1440`→`robot_history_1h`、V5 上限 400 等）仍 PASS | 单测 + CI |
| V5 | CI 双绿 | `pipeline-1(test)` + `pipeline-2(e2e-30k-landing)` 均 success（e2e 真绿：rows_written≥30000 且无 `【E2E SKIPPED】`） | `cnb pulls list-pull-commit-statuses` + 按需 `get-build-logs` |

---

## 6. 约束 / 禁止项

- 禁止引入 `history()` UNION `robot_history_1h` + `robot_history_10s` 补洞（GAP-1 误判修复，冗余且可能重复点）。
- 禁止设 `materialized_only=true` 或把 `robot_history_1h` 改建为 materialized-only。
- 禁止改 `robot_history_1h` 刷新策略（`init.sql:254-258` 的 `start/end_offset`/`schedule` 保持）。
- 禁止改 10s 路径 / `_parse_resolution`（R-03 §4 / §4.1 硬红线）。
- 禁止在 mock pool 单测里伪造实时回填（ISSUE #60）。
- `git diff` 白名单：`infra/init.sql`（仅注释）+ 新增真库测试文件；不得夹带其它模块改动。

---

## 7. 交付物（codebuddy 产出，主代理回收核验）

- `infra/init.sql`：`robot_history_1h` 块上方新增「实时聚合 / 禁 materialized_only=true」注释（无 DDL 语义变化）。
- 真 TimescaleDB 回归测试（如 `tests/e2e/test_cagg_realtime_backfill.py` 或并入现有 e2e）：灌 `[now-30min,now]` → 断言 `robot_history_1h` 查回。
- PR 的 `git diff` 白名单仅 `init.sql`（注释）+ 测试文件；CI 双绿（test + e2e-30k-landing 真绿）。

---

## 8. 关联

- 来源：`docs/GAP-SCAN-2026-10-13.md` GAP-1（已复核为误判，本需求为其防回归落地）。
- 依赖不变式：`robot_history_1h` 实时聚合（TimescaleDB 2.13.1 默认 `materialized_only=false`）。
- 上层契约：R-12（`/history` 长期趋势查询）+ R-03 §4（`/history` 响应契约）。
