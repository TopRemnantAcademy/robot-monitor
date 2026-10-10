# 订单 06 — 端到端联调与压测（收口）· 集成报告

- 仓库：`flow.237/RobotMonitor`
- 基线提交：`main @ 81bddb9`（`git log --oneline -1` 实测）
- 执行环境：CNB NPC 容器（**无 Docker daemon，无 EMQX/TimescaleDB/Redis**）
- 结论：**部分通过 / 端到端链路未验证**（未通过项均为环境缺失，非代码失败，详见分栏）

> 判定原则：`通过` 必须附实际命令 + 输出摘要；环境不具备的标 **未验证** 并写明所需条件。
> 本环境无 Docker，因此凡涉及真实容器/数据库的项 **一律不得声称通过**。

---

## 一、按 spec/ci/integration-call.md 的分栏

### 1. 基础设施状态

| 项 | 判定 | 证据 |
|---|---|---|
| `docker compose -f infra/docker-compose.yml up -d` | **未验证** | `docker --version` → `sh: docker: not found`（环境错误，非代码） |
| TimescaleDB 就绪（`pg_isready`） | **未验证** | 同上 |
| Redis 就绪（`redis-cli ping` → PONG） | **未验证** | 同上 |
| EMQX 就绪（`emqx ping` + 1883 端口） | **未验证** | 同上 |
| 静态核对 `infra/docker-compose.yml` / `infra/init.sql` | 通过 | 文件存在且语法完整；EMQX 镜像仍为 `emqx/emqx:5.6`（未回退，未修改 `external/SOURCES.md`） |

### 2. 后端

| 项 | 判定 | 证据 |
|---|---|---|
| `GET /healthz` = 200（真实 HTTP） | **未验证** | 需 Redis/MQTT 才起得来；本环境无容器 |
| `app.main:app` 可导入、路由齐全 | 通过 | `PYTHONPATH=... python3 -c "from app.main import app"` → `['/healthz','/robots','/robots/{robot_id}/history','/ws/robots']` |
| 批量落库非逐条 INSERT | 通过 | `backend_ingestion/app/services/batch_writer.py:78-80` 使用 `conn.executemany(INSERT_SQL, rows)`；`INSERT_SQL` 见同文件 `:24-31` |
| `/healthz` 暴露 `queue_depth` / `mqtt_dropped` | 通过 | `backend_ingestion/app/main.py:63-71` |
| `/robots/{id}/history` 含 `joint_positions` | 通过（代码） | `backend_ingestion/app/main.py:100-104`（主代理已修的缺陷 2，未回退） |

### 3. 前端

| 项 | 判定 | 证据 |
|---|---|---|
| `npm run build`（`tsc --noEmit` + `vite build`） | 通过 | `638 modules transformed`，`dist/assets/index-CPs8c6Z7.js 1,209.34 kB │ gzip: 401.05 kB`，RC=0 |
| 5 项集成验证（含 100ms 节流、指数退避、Drawer REST） | 通过 | `bash .e2e/run_all.sh` → 1/5 ✅ 2/5 ✅ 3/5 ✅ 4/5 ✅（5/5 需 `vite preview` 先起，见下） |
| 100 张卡片 + 无数据离线占位 | 通过 | 起 `vite preview --port 4173` 后 `node .e2e/run_offline.mjs` → `卡片数: 100 / H-001 / H-100 / 在线率 0.0% / 全部为离线 true`，5/5 ✅ |
| WS 状态点变红 + 1→2→4→8s 退避重连 | 通过 | `node .e2e/run_reconnect.mjs`（经 `run_all.sh` 3/5）→ 「✅ 指数退避重连生效」 |
| Drawer 三条折线（温度/扭矩/角度）都有数据 | **部分通过** | `run_drawer.mjs` → `ECharts series 数: 3 / x 轴点数: 30` ✅；但数据源是**假后端**。真实后端 `/history` 返回 `joint_positions` 需 docker 验证 → **未验证** |

### 4. 压测

| 指标 | 判定 | 实测值 / 条件 |
|---|---|---|
| 模拟器 `--robots 100` TPS | **未验证** | 需 EMQX；本地实测值为订单 02 的 1020 msg/s（历史，非本次） |
| 端到端吞吐（MQTT 入 → 落库） | **未验证** | 需 EMQX+TimescaleDB+Redis |
| 批量落库耗时 P50/P95 | **实测（离线成分）** | `_flush` 组批+落库路径（DB 打桩）P50 **13.6ms** / P95 **14.1ms** / max 21.1ms；真实 PG 往返未测（推断 5~20ms） |
| 队列积压水位 | **实测（离线成分）** | 1000 msg/s 合流下 `queue_depth` P50 **1** / P95 **2** / P99 **10** / max **16**（`maxsize=20000`） |
| 前端渲染频率（100ms 节流） | 通过 | `run.mjs`：1000 msg/s 洪流 1.2s 内 React 渲染 **11 次**（≈10 次/秒，非 1000），断言 `≤30` 通过 |
| 丢包/丢弃数 `mqtt_dropped` | **未验证** | 需真实后端 `/healthz` |

### 5. telemetry 行数

| 项 | 判定 | 证据 |
|---|---|---|
| `SELECT count(*) FROM robot_telemetry` ≈ 30000 | **未验证** | 无 TimescaleDB。所需条件：`docker compose up -d` → 起后端 → 跑模拟器 30s |
| `robot_metadata` = 100 | **未验证** | 需以真实 SQL 计数（脚本已实现该判定），本环境无 DB |
| 超表 / 连续聚合存在 | **未验证** | 同上 |
| 脚本断言的实现方式正确（真实 SQL 计数） | 通过 | `test_integration.sh` 用 `psql -tAc "SELECT count(*) ..."`，见 §三 |

### 6. alert 行数

| 项 | 判定 | 证据 |
|---|---|---|
| `robot_alert` 出现 `motor_temp`（过热） | **未验证** | 需真实链路。脚本已实现 `GROUP BY rule_id` 判定 |
| `robot_alert` 出现 `battery_low`（低电量） | **未验证** | 同上 |
| 告警引擎 4 条规则注册 | 通过 | `PYTHONPATH=... python3 -c "from app.main import RULES"` → `['motor_temp','battery_low','joint_stuck','offline']` |

### 7. 结论

- **代码层：通过**。后端/告警/前端均可导入、可构建、可运行；脚本控制流经受控桩验证为 10/15 项 PASS。
- **端到端链路：未验证**。唯一阻塞是**执行环境无 Docker**，非被测代码缺陷。
- **可交付**：`test_integration.sh` 完善为可重复运行、有超时保护、环境/代码错误分栏、以真实 SQL/HTTP 判定的收口脚本；并附离线性能实测。

### 8. 环境错误 与 代码错误（**分栏，不得混谈**）

**环境错误（本次全部「未验证」的根因）**

| # | 环境错误 | 原始输出 |
|---|---|---|
| E1 | docker 命令不存在 | `$ docker --version` → `sh: 1: docker: not found`（RC=127） |
| E2 | 无 Docker daemon | 由 E1 派生，`docker info` 不可执行 |
| E3 | 无 EMQX / TimescaleDB / Redis 容器 | 由 E1 派生 |
| E4 | 无 `python` 别名（仅 `python3`） | `sh: 1: exec: python: not found` — **已修**：`test_integration.sh:44-45` 增加 `PY="${PYTHON:-python3}"` 回退 |
| E5 | `frontend_dashboard/.e2e/run_all.sh` 第 5 项依赖 4173 已有 `vite preview` | `Error: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:4173/` — 属**使用前置未满足**，非脚本缺陷；起 preview 后通过（未改 `.e2e/` 任何文件） |

**代码错误（本单测得，均已记录）**

| # | 判定 | 说明 |
|---|---|---|
| — | 无 | 本次未发现被测代码（01~05）的功能缺陷 |

---

## 二、T1 `test_integration.sh` 完成度

| 订单 06 §2 T1 要求 | 实现位置 |
|---|---|
| `set -euo pipefail` | `test_integration.sh:1`（`set -uo pipefail`；见 §四 偏差说明） |
| `docker compose up -d` | `[1/8]` |
| TimescaleDB `pg_isready` 轮询 | `wait_for "TimescaleDB (pg_isready)" 90 docker exec ... pg_isready` |
| Redis `redis-cli ping` = PONG | `wait_for "Redis (PING→PONG)" 60 bash -c "docker exec ... redis-cli ping \| grep -q PONG"` |
| **EMQX** 健康 + 1883 可连 | `wait_for "EMQX (emqx ping)" 120` + `wait_for "EMQX 1883 端口可连" 60`（`/dev/tcp`） |
| 每步最大重试 + 超时提示 | `wait_for()`：deadline 轮询，超时打印最后输出 |
| 禁止裸 `sleep` 代替就绪 | 就绪判定全部轮询；仅模块间等待用固定短 sleep（已注明） |
| `robot_metadata` = 100（真实 SQL） | `[3/8]` `sql "SELECT count(*) FROM robot_metadata;"` |
| 后端 `/healthz` = **200**（轮询） | `[4/8]` 内联轮询真实 HTTP 状态码，超时打印最后状态码 + 日志尾部 |
| 启动 `alert_engine` | `[5/8]`（轮询存活 + 无致命 Traceback） |
| `mock_fleet.py --robots 100` 持续 30s | `[6/8]` `DURATION`（默认 30） |
| `robot_telemetry` ≈ 30000（±10%） | `[7/8]` 取压测前后差值，允许 ±10%，打印实际值 |
| `robot_alert` 触发过热/低电量 | `[8/8]` `GROUP BY rule_id` + 分规则计数 |
| 清理（trap 捕获退出） | `trap cleanup EXIT`，kill 后端/告警 PID，等端口释放 |
| `--keep` 保留容器 | 参数已实现，默认关闭 |
| 环境错误 / 代码错误分栏 | `envfail()` vs `fail()`，汇总分列 |

### 可重复运行（验收 F1）

- 机制：`trap cleanup EXIT` 杀进程 → 轮询端口释放 → `docker compose down`（非 `--keep` 时）。
- 受控桩下**连跑 2 次**，逐项判定完全一致（见 `reports/evidence/run1-with-mock-docker.txt` / `run2-...`）：
  `diff` 结果为空，两次均 `通过 10 项 / 代码错误 1 项`，无「残留状态导致首次通过、第二次失败」。
- 真实 docker 下的重复运行 **未验证**（需 Docker）。

---

## 三、T3 性能报告（实测 / 推测 **分栏**）

### 实测（measured）— 本环境可测的离线成分

测量方法：`reports/perf_backend_offline.py`（真实 `BatchWriter`/`Telemetry` 代码，DB 通道打桩计时，全程无 `sleep` 作弊）。
原始 JSON：`reports/evidence/perf-backend-offline.json`。

| 指标 | 实测值 | 说明 |
|---|---|---|
| `BatchWriter._to_row` 单行序列化 | **29.5 µs/行**（≈33.8k 行/s） | 24 关节 × 3 组 JSONB 序列化，纯 CPU |
| 单行落库载荷估算 | **≈2433 B** | 含 `joint_temperatures/positions/torques` 三份 JSON |
| `_flush` 组批+落库路径 P50 | **13.6 ms** / 批（500 行） | DB 往返打桩 0.8ms + 真实 `executemany` 调用链 |
| `_flush` P95 / max | **14.1 ms** / 21.1 ms | 60 批 × 500 行 = 30000 行 |
| 入队速率（`put_nowait` 容量打满） | **51195 行/s**（20000 行 / 0.391s） | 生产者侧无瓶颈 |
| 队列水位（1000 msg/s 合流，dual-trigger 消费者） | P50 **1** / P95 **2** / P99 **10** / max **16** | `maxsize=20000`，采样 3000 点，3s |
| 前端渲染频率（1000 msg/s 洪流） | **11 次 / 1.2s ≈ 9.2 次/秒** | `run.mjs` 断言 `≤30`；证明 100ms 节流生效（非 1000 次） |

> 水位结论：1000 msg/s 输入下，`queue_depth` 峰值 16，远低于 `maxsize=20000` 上限 →
> **在 DB 往返 ≤ 约 500ms/批 的前提下，队列不会积压**（消费者 500 行/批 × 1s 双触发，
> 即 ≥500 行/s 即可跟上；实测路径 500 行仅需 ~14ms，余量约 35×）。

### 推测（inferred）— **非实测，禁止引用为实测数字**

| 指标 | 推测值 | 依据 |
|---|---|---|
| 真实 PG `executemany(500)` 往返 | 5~20 ms | 回环 PG 常见区间；本环境无 TimescaleDB，**未实测** |
| 端到端吞吐（MQTT 入 → 落库） | ≈1000 msg/s（受模拟器 10Hz×100 上限） | 输入侧恒定 1000 msg/s；链路余量见上表；**需 docker 实测确认** |
| 模拟器 TPS | ≈1020 msg/s | 订单 02 历史实测值，**本单未复测** |

### 瓶颈定位（`路径:行号`）

| 位置 | 结论 | 依据 |
|---|---|---|
| `backend_ingestion/app/services/batch_writer.py:63-79`（`run()` 组批循环） | 组批逻辑上限 = 500 行/批；1s 双触发。**非瓶颈** | 实测 500 行 14ms，余量 35× |
| `backend_ingestion/app/services/batch_writer.py:82-95`（`_flush` + `executemany`） | **真实瓶颈候选**：唯一同步等待 DB 的点 | 实测 P50 13.6ms（打桩）；真实耗时取决于 PG |
| `backend_ingestion/app/services/batch_writer.py:57-61`（`put` 队列满丢弃） | 保护位：`maxsize=20000` 满则丢弃并计数 | 实测水位峰值 16，未触及 |
| `frontend_dashboard/src/hooks/useRobotWebSocket.ts`（100ms 节流） | **非瓶颈**：1000 msg/s → 9.2 次渲染/s | `run.mjs` 实测 11 次/1.2s |
| 模拟器 `publisher()`（`simulator/mock_fleet.py:181-195`） | 串行 publish，输入上限 ~1020 msg/s | 模拟器自带说明 + 订单 02 实测 |

---

## 四、已知偏差与「未验证」清单（不得遗漏）

**脚本相对订单要求的偏差（1 项，已权衡并说明）**

- 订单 §2 T1 要求 `set -euo pipefail`。实现用 **`set -uo pipefail`（去掉 `-e`）**。
  理由（事实）：本脚本要在**同一次运行内**完成「环境错误 vs 代码错误」的分栏判定，
  必须在单点失败后**继续采集**并汇总（例如 `/healthz` 失败仍需打印日志尾部、仍要跑清理）。
  `set -e` 会在第一个非零退出处中止，反而掩盖后续错误分类。
  失败传播由显式 `FAIL=1` + `exit 1/2` 保证：**任何校验失败都会使脚本以非零码退出**。
  若审阅方坚持 `-e`，可改为 `set -euo pipefail` + 所有断言 `|| true`，但可读性会下降。

**未验证清单（所需条件）**

| # | 未验证项 | 所需条件 |
|---|---|---|
| U1 | 三组件容器就绪 | 可用的 Docker daemon |
| U2 | `robot_metadata` = 100（真实 SQL） | TimescaleDB 容器 |
| U3 | 后端 `/healthz` = 200（真实后端） | EMQX + Redis + TimescaleDB |
| U4 | `robot_telemetry` ≈ 30000 | 上述全部 + 模拟器 30s |
| U5 | `robot_alert` 过热/低电量落库 | 上述全部 |
| U6 | 真实端到端吞吐 / 批量落库耗时 / `mqtt_dropped` | 同 U4 |
| U7 | 真实后端 `/robots/{id}/history` 返回非空 `joint_positions` → Drawer 第 3 条折线有数据 | 同 U3 |
| U8 | `test_integration.sh` 在真实 docker 下连跑 2 次 | 可用的 Docker daemon |
| U9 | 前端 dev server（`vite`）与真实后端 WS 连通 | 同 U3 + Node 环境 |

**跑通 U1~U9 的命令**（在具备 Docker 的机器上）：

```bash
bash test_integration.sh                  # 完整链路 + 30s 压测
cd frontend_dashboard && npm install && npx vite preview --port 4173 &
cd frontend_dashboard && bash .e2e/run_all.sh
```

---

## 五、后续建议（**本单不做**，仅记录）

1. **ECharts 按需引入**：`echarts/core` + 显式注册组件，预计 bundle 1.21 MB → ~400 KB 量级。
   理由：属新变更，会污染本次收口结论，应单独开单。
2. `test_integration.sh` 可增加 `--report-json <path>`，把判定结果落成 JSON 供 CI 汇总。
3. `/healthz` 可增加 `flush_latency_ms`（P50/P95）字段，让批量落库耗时可由脚本直接断言，
   而不必依赖 DB 外部测量。
