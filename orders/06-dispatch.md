# 派工单：订单 06 — 端到端联调与压测（收口）

> 本文件是**下发给 CNB NPC（@codebuddy）的派工正文**，同时入库留档。
> 派单命令：
> `cnb issues post-issue-comment --repo flow.237/RobotMonitor --number 6 --work-mode --body-file orders/06-dispatch.md`

---

@codebuddy 请接单实现**订单 06（端到端联调与压测）**。这是本项目的**收口单**，
不是写新功能，而是**用真实运行证据证明 01~05 真的能跑通**，并给出性能实测数字。

## 0. 当前仓库真实状态（主代理已实测，非推测）

- 基线提交：`main @ 81bddb9`（`git log --oneline -1` 可核对）
- 订单 01（基础设施）：已完成 —— `infra/docker-compose.yml`、`infra/init.sql`
- 订单 02（模拟器）：已完成 —— `simulator/mock_fleet.py`，本地实测 **1020 msg/s**，11/11 用例通过
- 订单 03（后端采集）：已完成 —— `backend_ingestion/`，静态检查 13/13
- 订单 04（告警引擎）：已完成 —— `alert_engine/`，规则用例 20/20
- 订单 05（前端大屏）：**已完成并合入 main** —— `frontend_dashboard/`
  - 主代理已实测：`tsc --noEmit` **0 错误**；`vite build` **成功**（638 modules，dist 产出）
  - 构建产物 `dist/assets/index-*.js` 约 1.21 MB（gzip 401 KB），主因是 ECharts 全量引入 —— 见 §5 可选优化

**主代理在本次收口前已修复 3 个已确认缺陷，请勿回退：**

1. `infra/docker-compose.yml`：EMQX 镜像名由 `emqx:5.6` 修正为 `emqx/emqx:5.6`。
   实测两个名字 digest 不同，裸名会拉到**非 EMQX 的同名镜像**。（证据见 `external/SOURCES.md`）
2. `backend_ingestion/app/main.py`：`/robots/{id}/history` 增加 `joint_positions` 字段。
   缺失时前端 Drawer 第三条折线「角度(rad)」恒为 null、图上永远空白
   （`frontend_dashboard/src/components/RobotDetailDrawer.tsx:93`）。
3. `backend_ingestion/app/services/state_cache.py`：Redis Hash 值类型还原，
   否则前端收到 `status_code:"1"` 字符串，状态色与热力图全错。

## 1. 必读文件（按顺序，不要跳读）

| 顺序 | 文件 | 读它的目的 |
|---|---|---|
| 1 | `AGENTS.md` | 全局工程纪律与性能护栏 |
| 2 | `orders/06-integration.md` | 本单原始任务与验收标准 |
| 3 | `spec/05_master_execution_playbook.md` | 阶段六定义 |
| 4 | `spec/ci/integration-call.md` | 集成报告必须采用的格式 |
| 5 | `spec/08` 若存在则读 `docs/requirements/R-08-acceptance-matrix.md` | 编号验收矩阵 A1–F6 |
| 6 | `external/SOURCES.md` | 已下载的官方参考源及对照结论 |
| 7 | `external/tsbs/README.md`（已 clone 到本地参考，仅本地可见） | TimescaleDB 官方压测方法论 |
| 8 | `test_integration.sh` | 现有骨架，本单要**完善**它而非重写 |

## 2. 任务分解

### T1 — 完善 `test_integration.sh`（可重复运行）

必须按序执行，每一步都要有**超时保护**与**失败即退出**（`set -euo pipefail`）：

1. `docker compose -f infra/docker-compose.yml up -d`
2. 等待**三个**组件就绪（缺一即后续假失败）：
   - TimescaleDB：`pg_isready` 轮询
   - Redis：`redis-cli ping` 返回 PONG
   - **EMQX**：1883 端口可连 + `docker compose ps` 健康态
   - 每个等待都要有最大重试次数与超时提示，**禁止裸 `sleep N` 代替就绪判断**
3. 校验 `SELECT count(*) FROM robot_metadata` **= 100**（真实 SQL，不是日志）
4. 启动 `backend_ingestion`，轮询 `GET /healthz` 直到 **HTTP 200**（不是 sleep 5）
5. 启动 `alert_engine`
6. 跑 `python simulator/mock_fleet.py --robots 100` 持续 **30s**
7. 校验 `SELECT count(*) FROM robot_telemetry` **≈ 30000**（允许 ±10%，给出实际值）
8. 校验 `robot_alert` 中**确实产生**了过热与低电量两类告警（按 `rule_id` 分组计数）

**脚本收尾必须做清理**：trap 捕获退出信号，停掉后端/告警进程；
提供 `--keep` 参数保留容器以便人工排查（默认关闭）。

### T2 — 前端连通性真实验证

前端 `tsc` 与 `vite build` 主代理已验证通过；本单要验证的是**运行时连通**：

- 起前端 dev server，配置 `VITE_WS_HOST` 指向本机后端
- 用 `frontend_dashboard/.e2e/` 下已有脚本（NPC 自产）验证：
  - 首屏出现 **100 张**机器人卡片（不是空白、不是 0）
  - WS 状态点为「已连接」
  - 点开某台机器人 Drawer，三条折线**都有数据点**（温度/扭矩/角度）
    —— 特别注意第 3 条「角度(rad)」，若恒为空说明 `joint_positions` 未生效
  - 断网重连：杀掉后端，观察状态点变红并按 1→2→4→8→10s 退避重连
- 若 `.e2e` 脚本跑不起来，**如实记录失败原因**，不得跳过该验收项

### T3 — 性能报告（压测工程师角色）

必须给出**实测数字**，区分「实测」与「推测」两栏：

| 指标 | 要求 |
|---|---|
| 端到端吞吐 | msg/s（MQTT 入 → 落库）实测值 |
| 批量落库耗时 | 每批 ms，取 P50/P95 |
| 队列积压水位 | `writer.queue.qsize()` 峰值（`/healthz` 已暴露） |
| 前端渲染频率 | 证明 100ms 节流生效（例如 store 每秒更新次数 ≈10，而非 1000） |
| 丢包/丢弃数 | `mqtt_dropped`（`/healthz` 暴露） |

- 瓶颈定位必须给 **`路径:行号`**
- 参考 `external/tsbs/` 的方法论，但**不要照搬其数据模型**（它不含 24 关节人形机器人负载）

### T4 — 结构化集成报告

按 `spec/ci/integration-call.md` 格式输出，必须包含以下**分栏**：

- 基础设施状态 / 后端 / 前端 / 压测 / telemetry 行数 / alert 行数 / 结论
- **「环境错误」与「代码错误」两栏分开列**，不得混为一谈
- 未通过的项标 **「未验证」** 并写明所需条件（例如「需 Docker Desktop 运行」）
- 每项「通过」必须附**实际命令 + 输出摘要**，禁止臆断

## 3. 验收标准（逐条可判定）

- [ ] `test_integration.sh` 可重复运行（连跑 2 次结果一致，有清理逻辑）
- [ ] 校验全部使用**真实 SQL 计数**或真实 HTTP 状态码
- [ ] 报告含全部 7 个分栏，环境错误/代码错误分列
- [ ] 性能报告「实测」与「推测」分栏，瓶颈给 `路径:行号`
- [ ] 前端 100 张卡片 + 3 条折线有数据得到**运行时证据**（截图或 e2e 输出）
- [ ] 未验证项明确标注，无遗漏

## 4. 禁止项（违反即打回）

1. **禁止**用「日志无报错」代替通过判定
2. **禁止**因某一模块未实现就整体判失败 —— 未实现标「跳过（阶段 X 未完成）」
3. **禁止**伪造数字：跑不出来就写「未验证 + 所需条件」
4. **禁止**修改 `external/SOURCES.md` 中记录的 EMQX 镜像修正结论
5. **禁止**为了让测试通过而放宽阈值（如把 30000 改成 300）
6. **禁止**删除 `frontend_dashboard/.e2e/` 下已有脚本

## 5. 可选优化（**不要在本单做**，仅记录到报告「后续建议」）

- ECharts 按需引入（`echarts/core` + 显式注册组件）可把 bundle 从 1.21 MB 降到约 400 KB 量级
- 理由：本单目标是**收口验证**，改打包配置属新变更，会污染验收结论

## 6. 交付方式

1. 建分支：`feat/order-06-integration`（基于最新 `main`）
2. 按 T1~T4 实现，提交信息写明实测数字
3. **必须尝试 push 分支并开 PR 到 main**
4. 若 push 因权限失败（如缺少 `repo-code:rw`）：
   - **如实报告**，写清错误原文
   - 把完整报告写进 issue #6 的评论里，**不要用「已推送」掩盖失败**
   - 上一单（订单 05）就是只推了分支没进 main，主代理才手工合并的 —— 不要重演

---

**定位：你是收口验收方，证据优先于结论。跑不通就写跑不通，这比假通过有价值。**
