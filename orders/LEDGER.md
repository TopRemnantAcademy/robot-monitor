# 派工与核验总览（RobotMonitor / CNB @codebuddy 调度）

> 生成时间：2026-10-11
> 仓库：flow.237/RobotMonitor（CNB），本地镜像 C:/Desktop/ROBOT/robot-monitor
> 调度方式：每单开 Issue（正文无触发词）→ `post-issue-comment --work-mode @codebuddy` 触发 NPC 构建。
> 纪律：构建 `status:success` ≠ 完成；必须回收分支核验方向后才算「方案无误」。

## 一、总表（12 单）

| 订单 | 集成缺口 | CNB Issue | Build SN | Build 状态 | 产出分支 | 方向核验 |
|------|---------|-----------|----------|-----------|---------|---------|
| 15 | I-13 鉴权 JWT/RBAC | —（本地） | — | 已合 main | main (ef6a9bb) | 本地落地+推送 |
| 16 | 可观测性（剩余 T1b/T2/T3/T4） | #55 | cnb-qpa-1k4k47cap | pending (workMode✓) | 待产出 | doc 已 reconcile 并推送 |
| 17 | I-14 告警闭环/ack | #39 | cnb-p1t-1k4k3b0i0 | success | auto/alert-closure-2e09 | ✓ 范围正确 |
| 18 | I-15 WS 契约即代码 | #40 | cnb-1d6-1k4k3b4ol | pending | auto/contract-as-code-18 | ✓ 范围正确 |
| 19 | I-17 外部源 22/22 | —（本地） | — | 已合 main | main | 已合入 init.sql |
| 20 | passlib/pkg_resources 修复 | #38 | cnb-9a2-1k4k34o6l | success | feat/order-20-passlib-pkgresources | ✓✓ 深度核验 |
| 21 | I-11 TSDB 调优 env | #42 | cnb-qti-1k4k3j2pn | success | feat/order-21-tsdb-tune-env | ✓ 范围正确 |
| 22 | I-12 可观测导出器 | #43 | cnb-vt5-1k4k3j71m | success | auto/observability-exporters-471a | ✓ 范围正确 |
| 23 | I-16 k6 MQTT 压测 | #44 | cnb-ml1-1k4k3jbj6 | pending | ⚠ 未识别 | 需跟进 |
| 24 | I-18 测试框架 | #45 | cnb-isf-1k4k3jfsd | success | feat/order-24-test-framework | ✓ 范围正确 |
| 25 | I-19 DR 容灾 | #46 | cnb-b80-1k4k3jk8a | success | feat/order-25-dr | ✓ 范围正确 |
| 26 | I-20 前端健壮性 | #47 | cnb-rsm-1k4k3jp2o | success | auto/order26-frontend-resilience | ✓ 范围正确 |

说明：
- Issue #41 未被本批占用（CNB 顺序另有他用）；本批 Issue 为 #38,#39,#40,#42–#47,#55。
- 标注 `pending` 的构建中，17/18 已实际产出分支（仅 23 #44 与 16 #55 尚无可回收分支）。

## 二、方向修正（派发前已复验，防止 @codebuddy 走偏）

1. **订单 16 doc（orders/16-dispatch.md）**：T1 标注为「订单 19 已完成」——`/metrics` 在 `backend_ingestion/app/main.py:133` 已落，`metrics.py` 已用 `robot_backend_*` 前缀的 5 指标（queue_depth / batch_flushes_total / telemetry_written_total / mqtt_received_total / mqtt_dropped_total）。**原稿 `robot_monitor_*` 命名作废**，本单仅剩 T1b（补 `ws_connections` + `history_latency` 2 指标）+ T2 前端计数器 + T3 OTel + T4 JSON 日志。§4「订单 11」→「订单 15」。已推 main（26cafdf）。
2. **订单 17/18 doc**：init.sql 行号（62→82、92→195）、`emit()` 53→87、WS `ws_robots` 250、RBAC「订单 11」→「订单 15」已复验修正（已推 4cd7ede）。
3. **订单 20**：`requirements.txt` 钉 `setuptools<81`（非 ≥81——后者移除 `pkg_resources` 致 passlib 崩）。已深度核验：注释完整、仅 11 行增量、不动其它依赖。

## 三、分支方向核验（success≠done，已回收 8 分支 `git diff --stat main...<branch>`）

| 分支 | 期望范围 | 实际改动 | 结论 |
|------|---------|---------|------|
| feat/order-20-passlib-pkgresources | requirements.txt 钉 setuptools<81 | 仅 requirements.txt +11 | ✓ 精确 |
| feat/order-21-tsdb-tune-env | TSDB 调优 env | docker-compose.yml +67 | ✓ |
| feat/order-24-test-framework | 测试框架（不动 pytest.ini testpaths） | .cnb.yml + alert_engine/tests + 前端 vitest/msw/setup | ✓ |
| feat/order-25-dr | DR + runbook | DR-RUNBOOK.md + infra/ops/pgbackrest/* + restore/verify 脚本 | ✓ |
| auto/contract-as-code-18 | WS 契约即代码 | ws-envelope.schema.json + main.py(ws_robots) + useRobotWebSocket + openapi 导出 + 测试 | ✓ |
| auto/observability-exporters-471a | 导出器同网络 | docker-compose.observability.yml +23 + verify_wiring | ✓ |
| auto/order26-frontend-resilience | 错误边界 + react-query(/history) | App.tsx + ErrorFallback + queryClient + robotHistory | ✓ |
| auto/alert-closure-2e09 | 告警 ack/unack + 分页 | alert_engine silence + backend_ingestion/app/alerts/* + init.sql + ack UI + 测试 | ✓ |

## 四、待办 / 风险（下一步）

1. **合并 8 个 success 分支**：经方向核验无误，应经 PR 合入 main。PR 由 @codebuddy 按订单指令开出；合并后须跑回归：`tsc --noEmit` + `vite build` + 后端 `import app.main`。
2. **订单 23（#44 / cnb-ml1）**：构建仍 pending 且无明确可回收分支，需跟进（k6 mqtt_pub.js + mock_fleet）。
3. **订单 16（#55）/ 18（#40）**：构建 pending，等 @codebuddy 产出分支后再回收核验。
4. **STATUS.md 遗留项**：e2e 30k 行需 Docker；第三方缺陷项（D-xxx）仍待清。属另一张清单，未随本批派工。

## 五、派工纪律（防再走偏）

- 触发：Issue 正文**不含** @codebuddy；仅 `post-issue-comment --work-mode` 触发（CNB 不在 issue body 触发 NPC）。
- 每单 doc 派前必须复验 file:line 与订单号（纪律第 7 条）。
- `success` 只是构建通过，**必须**回收分支 `git diff --stat main...<branch>` 核验范围后才算方向无误。
