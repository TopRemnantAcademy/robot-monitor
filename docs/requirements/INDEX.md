# 需求规格索引（INDEX）

> 本目录是 **RobotMonitor 需求规格的唯一事实来源（single source of truth）**。
> 每个需求一份 `R-xx-*.md`，本文件是总索引与模式约定。
>
> 最后更新：**2026-10-13**（模式切换：本地写需求规格 → `@//codebuddy/` 标记 → Bearer 头 push 到 CNB，由 codebuddy 读仓执行；本地**只写需求规格、不写业务修复代码**）。

---

## 一、模式约定（2026-10-13 生效）

原流程依赖 CNB 平台 `workbuddy` NPC 在 issue/PR `@mention` 后执行；实测该 NPC
**4h+ 对派单无响应**，故弃用，改为：

1. **本地**撰写详细需求规格（本目录 `R-xx-*.md`），含：
   - 背景（**file:line 实证**，不得凭记忆）
   - 目标 / 范围边界（明确不做项）
   - 任务分解
   - 验收标准（可判定 `PASS/FAIL/未验证`，附真实命令 + 输出）
2. 在文档顶部打 **`@//codebuddy/`** 指派标记（CNB NPC 句柄，非本地路径）。
3. 本地 `git commit` 后，用 **Bearer 头** push 到 `cnb` 远程，使 codebuddy 可读仓执行。
4. 本地**不写业务修复代码**；只做：写需求 / 只读验证 / CI 核查 / issue 对照 / 合并回收。

> 触发 codebuddy 实际执行的机制沿用 G-7 已验证路径：在对应 CNB Issue/PR 评论里
> `@codebuddy` 并引用本目录规格（work-mode 派单已实测可产出分支 + PR）。

---

## 二、需求清单（R-01 ~ R-10）

| 编号 | 标题 | 状态 | 文件 | 实证 |
|---|---|---|---|---|
| R-01 | 前端监控大屏构造规格 | ✅ 订单 05 已实现并合入 `main`；本文件为**回归验收基线** | `R-01-frontend-dashboard.md` | `tsc --noEmit` 0 错误、`vite build` 成功（主代理实测） |
| R-02 | WebSocket 接入与 100ms 节流需求（前端·防卡顿核心） | ✅ 配套 R-01，订单 05 实现 | `R-02-websocket-throttle.md` | `useRobotWebSocket.ts` 节流契约 |
| R-03 | 后端接口契约（前端/联调唯一事实来源） | ✅ 订单 03 实现（`backend_ingestion/app/main.py`） | `R-03-backend-api-contract.md` | `GET /healthz` `/robots` `/history` `/ws/robots` 契约固化 |
| R-04 | — | ⚠️ **缺口（未建立）** | — | — |
| R-05 | — | ⚠️ **缺口（未建立）** | — | — |
| R-06 | — | ⚠️ **缺口（未建立）** | — | — |
| R-07 | 端到端联调与压测规格 | ✅ 订单 06 收口单（判据本身） | `R-07-integration-and-stress.md` | `test_integration.sh` 可重复；30,000 行实跑（CI 承证 #111） |
| R-08 | 验收测试矩阵（逐用例编号·可判定） | ✅ 订单 01–06 映射 | `R-08-acceptance-matrix.md` | 通用规则 + 7 段矩阵 |
| R-09 | 数据库对接规格（TimescaleDB） | ✅ Issue #19 已合入 `main` | `R-09-database-integration.md` | 真 TimescaleDB 2.13.1 实测 13/13；三级写入降级链 |
| R-10 | CNB → GitHub 单向自动同步（C4 / Issue #64） | ⏳ **流水线已落地，端到端未验证**（缺 GitHub 凭据 + `flow.237/RobotMonitor-secrets` 密钥仓库） | `R-10-cnb-github-sync.md` | `.cnb.yml:23-45` 已挂 `push` 事件；同步未跑通 |
| R-11 | 业务 Grafana 看板固化（robot_backend_*/robot_alert_* 可视化） | ⏳ **待执行（新需求）** | `R-11-business-grafana-dashboard.md` | 9 个 EMQX 看板已存；业务指标 `grep` 0 命中（实证 §1） |
| R-12 | `/history` 长期趋势查询（小时级下采样，跨天可见） | ⏳ **待执行（新需求/优化类·中风险）** | `R-12-history-longterm-trend.md` | `history()` 仅 10s cagg + 原始回退，`minutes` 无上限（`main.py:278`） |

> R-04/05/06 为编号空档：历史上预留给「前端构建优化 / 告警抑制 / Prometheus 指标接入」，
> 但 C1(#62)/C2/C3 已在 `STATUS.md` 标注完成，故这三份规格**未单独成稿**。如需补建，
> 由下一轮需求派单触发（见第三节）。

---

## 三、关联草稿（issue_bodies/，已落地，待清理）

`issue_bodies/01-*.md` ~ `08-*.md` 是**派工前的中间草稿**，核验后发现其描述的问题
**已全部在 `main` 上实现**（对应 Issue #71–#78）。这些草稿已无执行价值，列此备查，
**不应**再正式化为 R-* 需求：

| 草稿 | 主题 | 已于 `main` 落地（实证） | 对应 Issue |
|---|---|---|---|
| `01-ws-fanout.md` | WS 服务端广播未合并（每连接独立 pubsub） | ✅ 进程级 `ws_hub` 中枢 + 100ms 合并（`main.py:562-590`） | #71 + #73 |
| `02-history-raw-table.md` | `/history` 直查原始 hypertable | ✅ 优先查 `robot_history_10s` 连续聚合 + 空则回退（`main.py:252-343`） | #76 |
| `03-telemetry-map-copy.md` | `applyTelemetryBatch` 每帧整 Map 拷贝 | ✅ 结构性共享，引用级去重探测（`robotStore.ts:156-175`，ISSUE #75） | #75 |
| `04-phantom-online.md` | 离线判定仅依 `status_code===0`，停报机器人永显在线 | ✅ `is_online`/`last_seen` 三分语义（`robotStore.ts:93-97,206-232`，ISSUE #77）；后端 `metadata_writer.py` 批量写 | #77 + #96 |
| `05-ws-backpressure.md` | `/ws/robots` 无背压/心跳 | ✅ `_ws_watchdog` 慢客户端摘除 + 半开检测（`main.py:430-512`，ISSUE #73） | #73 |
| `06-resolution-truncation.md` | `_parse_resolution` 静默截断 `1.5m→90s` | ✅ 非整数倍显式 400（`main.py:371-416`，ISSUE #74） | #74 |
| `07-metrics-dual-naming.md` | `robot_backend_*` 与 `robot_monitor_*` 双轨并存 | ✅ 收敛为 `robot_backend_*` 单一命名（`metrics.py:25-48`） | #72 |
| `08-ci-npm-test-gate-red.md` | `npm test` 8/8 全红（缺 QueryClientProvider） | ✅ `setup.ts` 全局包裹 `QueryClientProvider`（ISSUE #78） | #78 |

---

## 四、待派需求（主动扫描发现的真实缺口）

> 2026-10-13 主动扫描：排除已落地（#71–#78）的 `issue_bodies/01-08` 草稿、及已收口的
> G-/C-/A- backlog 后，以下为**真实开放**项。R-11/R-12 已写好规格，待本地 commit +
> Bearer 头 push 到 CNB 后由 codebuddy 读仓执行。

| 编号 | 主题 | 状态 | 阻塞 |
|---|---|---|---|
| **R-11** | 业务 Grafana 看板（robot_backend_*/robot_alert_* 可视化） | ✅ 规格已写，待 push + codebuddy 执行 | 无（纯新增 JSON，低风险） |
| **R-12** | `/history` 长期趋势查询（小时级下采样） | ✅ 规格已写，待 push + codebuddy 执行 | 无（改 `init.sql`+`main.py`，中风险，R-03 回归护栏为硬门槛） |
| R-10 | C4 GitHub 同步端到端验证 | ⏳ 规格在 `R-10-cnb-github-sync.md` | **需用户备 GitHub PAT（只写 `robot-monitor`）+ 建 CNB 密钥仓库 `flow.237/RobotMonitor-secrets`**；本地无法执行 |

> 其余 backlog（G-1~G-7、C1~C3、A1~A3）经 `STATUS.md` 对账 + 代码实证均已收口或转为已落地规格。
> 下一轮需求派单继续在本目录新增 `R-13-*.md` 起，并遵循第一节模式约定。
