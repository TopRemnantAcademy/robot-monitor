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

## 六、第二轮派发（2026-10-11，Issue #58–#64）

本批从 STATUS.md 的「后续增强 C1/C4」「非功能缺口 G-3/G-4/G-6/G-7」与「仍未验证 e2e 30k」中 harvest 出 7 个尚未派发的真实问题，逐个开 Issue（正文无触发词）+ `--work-mode @codebuddy` 派发。

| Issue | 问题 | 缺口 | Build SN | Build 状态 | 方向校准要点 |
|-------|------|------|----------|-----------|-------------|
| #58 | WS 合成探针（识别假绿） | G-3 | cnb-sar-1k4k4q3nj | success✓ | 新增 ws_probe_alive/failures_total；与订单16 T1b 的 ws_connections 不冲突 |
| #59 | 告警落库失败可观测 | G-4 | cnb-nl1-1k4k4q3nk | success✓ | alert_engine 加 robot_alert_store_errors_total + Alertmanager 规则 |
| #60 | e2e 30k 落库 Docker 验证 | 仍未验证#1 | cnb-a2r-1k4k4q3mq | pending | 真实 TimescaleDB，断言 >=30000，禁 mock 替身 |
| #61 | robot_metadata last_seen/is_online 写入 | G-6 | cnb-hn1-1k4k4q3m8 | pending | 批量化更新，offline 规则读 is_online 列 |
| #62 | 前端 ECharts 按需引入瘦身 | C1 | cnb-579-1k4k4q3jt | pending | echarts/core 按需 use，禁 CDN/删实际图表 |
| #63 | 任务级多机器人协同仿真 | G-7 | cnb-d5s-1k4k4q3e7 | pending | simulator/task_sim.py，禁假相关/GPU |
| #64 | CNB→GitHub 自动同步 | C4 | cnb-n4i-1k4k4q3c0 | pending | .cnb.yml job + secret 注入，禁明文 token |

> 7 个构建均已触发（00:11:41Z），workMode=true 已核验（抽样 cnb-sar 为 success+workMode✓）。AI 积分未耗尽，派发链路正常。

## 七、累计待办 / 风险（截至 2026-10-11）

1. **第一轮 8 个 success 分支待合并**（feat/order-20/21/24/25 + auto/contract-as-code-18 / observability-exporters-471a / order26-frontend-resilience / alert-closure-2e09）：方向已核验无误，经 @codebuddy 的 PR 合入 main 后跑回归 `tsc --noEmit` + `vite build` + `import app.main`。
2. **第二轮 7 单（#58–#64）待回收核验**：等 @codebuddy 产出分支后按 SOP 三点 diff。
3. **订单 23（#44 / cnb-ml1）** 仍 pending 且无明确分支，需跟进（k6 mqtt_pub.js + mock_fleet）。
4. **订单 16（#55）/ 18（#40）** 构建 pending，等产出分支。
5. STATUS.md 的 e2e 30k（#60 已覆盖）、第三方 D-xxx 缺陷属另一清单，未随本批派工。

## 八、集成合并结果（integrate/round1，2026-10-11 落地）

> 策略：无 PR（NPC 只推分支未开 PR）→ 建本地 `integrate/round1`（从 main@1f6fc9f）逐分支 `git merge --no-ff`，每分支 `git diff --stat main...<branch>` 核验范围后合并；冲突按「并存/union」原则手工解决。最后回归 + 合入 main + Bearer 推送 cnb。

### 8.1 已合并分支（全部经三点 diff 核验范围）

| 批次 | 分支 | 合并提交 | 冲突解决要点 |
|------|------|---------|------------|
| 第一轮 | feat/order-20-passlib-pkgresources | 09f69f1 | 无（仅 requirements.txt） |
| 第一轮 | feat/order-21-tsdb-tune-env | e95403c | 无 |
| 第一轮 | auto/observability-exporters-471a（订单22） | 802d41c | 无 |
| 第一轮 | feat/order-25-dr | dceb7ed | 无 |
| 第一轮 | cb/order16-observability-t1b-t2-t3-t4 | b9243d8 | .env.example（TimescaleDB 段 ∪ OTel 段） |
| 第一轮 | auto/alert-closure-2e09（订单17） | 0e6a4a9 | main.py import（metrics,telemetry_tracing ∪ alerts_router） |
| 第一轮 | auto/contract-as-code-18（订单18） | 280329b | main.py import（logging_json ∪ schemas.api） |
| 第一轮 | feat/ws-synthetic-probe-58（#58） | ad7eb1d | main.py lifespan（telemetry ∪ probe_task）、README、verify_wiring.sh（并存） |
| 第一轮 | feat/issue-59-alert-store-metrics（#59） | — | robot-alerts.yml / verify_wiring.sh（ort 自动并存） |
| 第一轮 | feat/order-24-test-framework（订单24） | b456cff | package.json（contract 脚本 ∪ test 脚本）、devDeps（openapi-typescript ∪ msw）；package-lock.json 用 `npm install --package-lock-only` 重新生成 |
| 第一轮 | auto/order26-frontend-resilience（订单26） | 4e47904 | package-lock.json 重新生成；App.tsx / ErrorFallback 自动合并 |
| 第一轮 | auto/e2e-30k-harness-7687（#60） | — | .cnb.yml / STATUS.md（ort 自动并存） |
| 第一轮 | auto/cnb-github-sync-34b2（#64） | 5dfe971 | .cnb.yml：`main:` 块合并，push 下列表含 test（订单24）∪ sync-to-github（#64） |
| 第一轮 | feat/order62-echarts-gauge-bar（#62） | — | 无（echartsCore.ts + README） |
| 第一轮 | codebuddy/order23-k6-load（订单23） | — | 无（tests/load/ 全新 + .gitignore） |

（#59/#60/#62/#23 合并提交哈希随 `--no-ff` 一并进入 integrate/round1，上表仅列显式验证过的提交。）

### 8.2 回归结果

- **前端**：`tsc --noEmit` ✅ 0 错误；`vite build` ✅（954 modules，dist 750KB / gzip 250KB）。
- **后端**：`python -m py_compile` ✅ 全部 backend_ingestion/alert_engine `.py` 语法通过；`main.py` 合并引入的符号（`telemetry_tracing.*` / `logging_json.configure_json_logging` / `alerts.router` / `schemas.api.*` / `ws_probe.run`）逐项确认在各自模块有定义；`batch_writer`/`mqtt_subscriber` 的指标调用与 `metrics.py` 导出对齐。
- **后端 `import app.main` 实测 ⚠️ 环境阻断**：本机 Win-ARM64 + Python 3.13 无 `pydantic-core` 轮子且需 Rust 编译，venv 安装失败。**此步骤留待 CNB CI（python:3.11 镜像有现成轮子）或兼容主机复跑**，不视为代码缺陷。

### 8.3 待决 / 风险（需用户拍板或跟进）

1. **🔴 订单 16 命名偏差（robot_monitor_* 并存）**：派工 doc（orders/16-dispatch.md）写明「原稿 robot_monitor_* 命名已作废」，但 @codebuddy 的分支在 `metrics.py` 新增 6 个 `robot_monitor_*` 指标（batch_queue_size / batch_flush_total / batch_rows_total / mqtt_messages_total / ws_connections / history_latency_seconds），与订单 19 已落地的 5 个 `robot_backend_*` **并存**。其中 batch_queue_size / batch_rows_total / mqtt_messages_total 与既有 robot_backend_* **语义重复**（同一计数源）；仅 ws_connections / history_latency_seconds 是 T1b 真正新增。分支内注释主张「两套语义不同、并存不替换」。→ 待用户决定：接受并存，或合并为 robot_backend_* 单一命名（推荐后者以消重）。
2. **🟡 #61（G-6 last_seen/is_online）分支缺失**：`ls-remote` 中无对应分支，构建 #61（cnb-hn1）状态 pending → 需回查 build 日志或重新派发。
3. **🟡 #63（G-7 任务级多机器人协同仿真）分支缺失**：同上，分支未入远端的 `simulator/task_sim.py` 形态，构建 #63（cnb-d5s）pending → 需跟进。
4. **🟢 #60 e2e 30k / #64 GitHub 同步**：已合并，但二者构建在派发时标记 pending；落地后建议各跑一次 CNB CI 确认门禁绿。
5. **🟢 .cnb.yml `main:` 分支作用域**：订单 24 的测试门禁与 #64 同步均挂在根级 `main:` 键下（理解为「main 分支作用域」）。若 CNB grammar 不认 `main:` 作分支作用域，则测试门禁/sync 可能不触发；建议首次推送后查一次 CNB 构建日志确认。

## 九、UI 强化专项（2026-10-12，主题锁死 dark-industrial，只加信息密度不改色）

> 执行方式：本地实现 + CNB PR/CI 门禁 squash 合并。订单 27 原计划派发 @codebuddy，
> 因 CNB AI 积分耗尽（402，crontab 构建 cnb-uhp/cnb-pt8 同因挂起）改为本地直做。

| 订单 | 内容 | Issue | PR | 合并 | 核验 |
|------|------|-------|----|------|------|
| —（UI R1） | 机群健康分析条：状态分布/电量直方/温度直方/双轴趋势（200ms 采样入 store） | —（本地） | #93 | squash 7f355c7 | 本地四门禁 + CI 绿 |
| —（UI R2） | Drawer 诊断深化：四 series 折线（含包络）/电量趋势图/基本信息 6 格/本机告警区（robot_id 过滤） | —（本地） | #94 | squash c7719f5 | 本地四门禁 + CI 绿 |
| 27 | Drawer 第三轮：24 关节扭矩负载条（纯 CSS）+ 告警确认/撤回按钮（admin）+ 历史窗口 5/30/60m | —（本地） | #95 | squash e768ab4 | 本地四门禁绿（tsc 0 / vitest 17 / build / gzip 246.35kB）+ CNB CI 双 pipeline 绿（test 2m22s / e2e-30k 2m4s） |

订单 27 规格存档：`orders/27-dispatch.md`（随 PR #95 分支入库）。关键约束：
- 扭矩条 aria-label 前缀「扭矩 」，与热力图 `/^left_elbow_pitch/` 正则互斥（测试契约）
- ack 错误文案禁含 "HTTP 5"（500 用例唯一性）
- minutes 进 queryKey（`['robot-history', robotId, minutes]`），切窗口零手写 effect
- 零新增 ECharts 实例；后端零改动（`/history` minutes 无上限为本单前提，main.py 已核实）

## 十、最大化 CNB 派单（2026-10-13）

> 经费状态变更：本轮开工时先做一次探针（`post-issue-comment --work-mode` 于 #96）→ 构建
> `cnb-u9v-1k4od99jn`（event=`issue.comment@npc`）成功拉起且 `workMode: true`
> —— 2026-10-11/12 的 402 配额已恢复，随即 6 单全派。
> **教训**：别用「上次 402」推断现在不可用，每次会话重探只需一个 comment。

### 10.1 派单与回收结果（6 单）

| 订单 | 内容（来源=实证） | Issue | PR | 合并 | 核验结论 |
| --- | --- | --- | --- | --- | --- |
| 28 | G-6 元数据心跳 + offline 改读 is_online（打捞 `auto/metadata-heartbeat-3f2a@0dba075f`） | #96 | #106 | squash 91d5b70 | ✅ 净增量到位（metadata_writer 205 + 3 测试文件），CI 双绿 |
| 29 | DB 连接池/时序采样测试打捞（素材 `auto/e2e-30k-harness-7687@23e6c6af`） | #97 | #103 | squash 3bb52dd | ✅ 合并。**超出授权**两项已接受并注明：改 `.cnb.yml` 补 `rich==13.7.1`（让 3 用例从 SKIP 变真跑）、改 `mock_fleet` 加毫秒发号器（直指 STATUS 缺陷 #4 主键冲突静默丢行） |
| 30 | 告警抑制 inhibition（severity 分层 + 可追溯） | #98 | #104 | squash a59e890 | ✅ 合并（inhibition.py 143 + 183 行测试；对既有用例仅"关闭抑制以隔离被测路径"，未弱化断言） |
| 31 | G-5 pgBackRest 真实备份收口 | #99 | #108 | squash 8cbe4f7 | ✅ 合并（backup.sh / verify.sh / verify-restore.sh + exporter queries + Dockerfile.timescaledb，1266 行） |
| 32 | 指标命名去重（LEDGER §8.3-1） | #100 | #102 | squash 77ec64c | ⚠️ **首轮拒收后返工合格**：首轮 diff 仅 2 个文档、**零代码改动**；重派（补「产出文件白名单」硬门槛）后 9 files/+377−17：两处 `metrics.py` 真改名（`robot_alert_robots_online` → 加 `_gauge` 类型自证）+ observability 引用同步 + `.cnb.yml` 自建**运行时/全仓两级硬门禁**。CI 双绿；重基后与主代理三道门禁共存 |
| 33 | 前端大机群渲染护栏 | #101 | #107 | squash e9c22aa | ✅ 合并（含"反向护栏"用例自证渲染量统计仪器非摆设；286 行 / 11 处断言） |

backlog 三个实证来源：① `git diff --stat main...(两个保留分支)` ② `STATUS.md:77-98`（C2/C4/G-5/G-6）③ `orders/LEDGER.md §8.3-1`。没有证据的单一律不开。

### 10.2 不耗积分、永久交给 CNB 的部分（PR #105 → squash ad0fbfe）

固化进 `.cnb.yml` 的 `test.pytest` stage（push 与 pull_request 逐字一致）：

| # | 门禁 | 补的洞 | 本机实测 |
| --- | --- | --- | --- |
| ① | `compileall -q` 全量语法编译 | 测试收集不到的文件里的语法错从不显形 | rc=0 |
| ② | 每个 `test_*.py` 必含 ≥1 个真实 `def test_` | STATUS.md §五手工扫过的「假覆盖测试文件」变机器门禁 | 207 文件 0 违规 |
| ③ | 两服务 `import app.main` 冒烟 | LEDGER §8.3-5：本机 Win-ARM64 无 pydantic-core 轮子，只能 CI 承证 → **本项就此关闭** | 本机确无轮子 |

### 10.3 没能交给 CNB 的两项（诚实标注）

| 项 | 结论 | 证据 |
| --- | --- | --- |
| #64 CNB→GitHub 自动同步 | **维持暂停** | 昨日「匿名推送成功」结论**不准确**：本次 `GIT_TERMINAL_PROMPT=0` + 空 credential.helper 实跑 → `fatal: could not read Username for 'https://github.com'`。真正生效的是本机凭据而非匿名写入。注入凭据须用户授权 |
| NPC crontab（6 段） | 仍注释；配额已恢复，待 6 单收口后评估 `cnb build build-crontab-sync` | 「跳过」≠「跑过」同样适用于此：恢复前先确认不会再每 6h 红一条 |

### 10.4 派单纪律修订（本轮血亏换来的一）

**所有派工单必须补「产出文件白名单」硬门槛**：写明 `PR 的 diff 必须包含 X/Y 文件`，且描述必须贴
「改前/改后 命令输出」对照。订单 32 的验收 [ ] 清单写得再漂亮，缺了这条就让「只改文档」也能自洽通过。

## 十一、继续派单 + NPC 自主流水线复活（2026-10-13 上午，用户「在CNB里 @//codebuddy/ 替我干活！不要在本地干。继续」）

### 11.1 Ops（主代理直做，非工程活）

| 项 | 结果 | 证据 |
| --- | --- | --- |
| 素材分支删除 | ✅ `auto/metadata-heartbeat-3f2a`、`auto/e2e-30k-harness-7687` 已删 | 先验证三份独有文件均在 main（`git cat-file -e main:…` ×3 ✓），内容已榨干 |
| NPC crontab 恢复 | ✅ 6h×7 段流水线复活 | `4552492` uncomment → `build-crontab-sync --branch main` 返回 "successfully synchronized" |
| ⚠️ 一次返工 | `c4252b3` 首推 422 CONFIG_ERROR | **pyyaml 宽容、CNB js-yaml 严格**：uncomment 后键缩进 3sp，pyyaml 照单全收、CNB 拒收。`4552492` 修正为 2sp 过审。教训：**CI 配置校验要用 CNB 自己的 build-crontab-sync 当真理，本地 pyyaml 通过≠过审** |

### 11.2 新派两单（均 `--work-mode` 触发，workMode=true 已核验）

| 订单 | 内容 | 依据（取证） | Issue | NPC 构建 | 结果 |
| --- | --- | --- | --- | --- | --- |
| 34 | e2e 假绿专项：SKIP 静默变绿 + 镜像声明失效归因修复 | `cnb-st7-1k4onp4ok-001` 日志实锤：e2e stage 仅 240ms、SKIP（缺省镜像）却 success；crontab 块 e2e 漏 `docker.image`；push 块声明了 image 却仍跑缺省镜像（归因未明，正是要派出去的活） | #109 | cnb-c71 → PR #111 | ✅ **已收口**：squash 合并 → main `f41a90a`；硬门槛达成（e2e 实跑 `rows_written=39785 ≥ 30000`，非 SKIP）；CI 双绿（test 165+26 passed / e2e 真跑）；白名单三点 diff 核验仅 5 文件；分支已删、Issue #109 关 completed |
| 35 | UI 第四轮：告警「抑制」呈现（全栈） | `grep -rn 抑制 frontend_dashboard/src` = 0 命中；订单 30 的可选 T4 确认未做；WS 连接态 chip 已存在（不重派） | #110 | cnb-mnr（首轮挂起）→ 重派 cnb-4m1-1k4pdpcnl → PR #112 | ✅ **已收口**：squash 合并 → main `4487101`；T1 判 c 路（后端落字段+契约再生）；CI 双绿 + e2e 真跑 39830 行；契约门禁绿（api.gen.ts 经 openapi-typescript 再生，非手改）；白名单 `merge-base..branch` 两点 diff 仅 20 文件核验通过；分支已删、Issue #110 关 completed |

两单均带**产出文件白名单**硬门槛（订单 32 血亏教训的制度化）。

### 11.3 订单 35 首轮 workMode 构建挂起 → 重派（2026-10-13 上午）

- 首轮 `--work-mode` 触发（cnb-mnr-1k4pbnigd）停在 `npc go` 阶段（duration 0、无分支、无 PR），与同批订单 34 的 cnb-c71（4 分钟内产出 PR #111）形成对照 → 判定为**孤立挂起**，非排队。
- 处理方式：在 issue #110 追加一条新的 `--work-mode` 评论重派（issue 正文不含 `@codebuddy`，触发干净、无双触发陷阱）；新构建 `cnb-4m1-1k4pdpcnl` 立即以 `sourceRef: feat/order-35-suppressed-ui` 出现，且已越过 `npc go`、进入 CI（test/pytest + e2e-30k-landing prepare）→ 证实重派生效。
- **教训**：workMode 构建若长期停在 `npc go`（duration 0）且同批其他单已产出 PR，应视为挂起而非慢；可安全重派，重派评论本身不造成重复提交（NPC 以最新构建为准）。监控只看「有没有分支/PR」比看 stage 文本更可靠。

### 11.4 合并核验补充纪律（本轮踩坑）
- **`refs/cnb/main` 本地陈旧分支是隐形炸弹**：本仓存在本地分支 `refs/cnb/main`（58513490，远古），会遮蔽 `refs/remotes/cnb/main`（当前 main）。任何写 `cnb/main` 的 `git diff/rev-parse` 都会落到陈旧基线，导致三点 diff 把订单 28-33/34 当「新增」（70 文件假象），并让 `grep &e2e-image` 在陈旧 .cnb.yml 上误报 0。已 `git update-ref -d refs/cnb/main` 删除。**此后一律用 `main`（本地）/ `HEAD` / 完整 SHA / `refs/remotes/cnb/main`，绝不裸写 `cnb/main`。**
- **两点 diff 才是真增量**：派工分支多从旧 main 派生，`git diff A...B`（三点，对 merge-base）会把派生点以来的 ALL 历史当新增。核验白名单必须用 `git diff <merge-base>..<branch>`（两点）或 `git diff main..branch`（HEAD=当前 main），只看本单增量。本轮订单 34/35 均靠此法确认白名单干净（订单 34 仅 5 文件、订单 35 仅 20 文件）。
- **squash 合并不会回退既有文件**：订单 35 分支派生早于订单 34 合并，但其 `merge-base..branch` 不含 .cnb.yml，故 squash 到 main 后订单 34 的 `&e2e-image` 锚点（.cnb.yml:216/452/558）完好——已用 SHA 直接 `git show 4487101:.cnb.yml` 复核，非依赖易遮蔽的 ref。

