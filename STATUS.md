# STATUS — RobotMonitor 主计划与状态看板

> 最后更新：**2026-10-11（第二轮全量对账 22 个分支 + 派出订单 19）**。
> 派单方法见 `docs/CNB-DISPATCH-SOP.md`（**§6.5 批量回收纪律**为本轮新增，
> 步骤 6~8 为第二轮补充：三点 diff、超集校验、假覆盖扫描）。
> 上游开源源套用计划见 `external/INTEGRATION-PLAN.md`。
> 用真实源码复核三条审计结论的报告见 `external/ORDER12-VERIFICATION.md`。

---

## 一、项目目标（一句话）

**100 台人形机器人的 2D 实时监控大屏**（在线状态 / 电量 / 关节电机温度 / 扭矩 / 故障码），
端到端 100 台 × 10Hz = **1000 msg/s**，全部编码工作交给 CNB 的 @codebuddy 执行。

明确不做：3D CAD、数字孪生。

---

## 二、当前真实状态（实证，非推测）

| 模块 | 状态 | 证据 |
|---|---|---|
| 01 基础设施 | ✅ 完成 | `infra/docker-compose.yml`、`infra/init.sql`（100 台播种） |
| 02 模拟器 | ✅ 完成 | `simulator/mock_fleet.py`，实测 **1020 msg/s**，用例 11/11 |
| 03 后端采集 | ✅ 完成 | `backend_ingestion/`，静态校验 13/13 |
| 04 告警引擎 | ✅ 完成 | `alert_engine/`，规则逻辑 **20/20** 实测 |
| 05 前端大屏 | ✅ **已完成并合入 main** | 34 文件/6696 行；`tsc --noEmit` **0 错误**、`vite build` **成功**（638 modules） |
| 09 数据库对接加固 | ✅ **已合入 main** | 真 TimescaleDB 2.13.1 实测：`init.sql` 全程无错、30,000 行落库无损、连续聚合 3,100 桶、DB 用例 **13/13**。含**三级写入降级链**（L1 COPY→临时表→`ON CONFLICT` / L2 `executemany` / L3 二分隔离坏行）与 **JSONB 类型修正**。见 `docs/requirements/R-09-database-integration.md` |
| 06 联调压测 | ✅ **已回收** | 证据落 `reports/evidence/`（perf 数据 + 3 次实跑日志）；`test_integration.sh` 已加固超时守卫 |
| 07 aiomqtt 契约（I-1/I-2/I-3） | ✅ **已回收验证** | `aiomqtt==2.5.1`、`_STOP` 哨兵 + `_drained` 事件就位 |
| 08 asyncpg 升级（I-6） | ✅ **已落地** | `asyncpg 0.29.0 → 0.32.0`；实测 `create_pool()` 同时接受 `init_size` 与 `min_size`（语义护栏见 `app/db/pool.py`） |
| 10 告警抑制 inhibition | ✅ **已回收验证** | `alert_engine/tests/` **11/11 passed** |
| 前端 bundle 瘦身 | ✅ **已回收验证** | 1209.34 → **671.40 kB**（gzip 401.05 → **224.90 kB**，−44%） |
| 62 前端 ECharts 注册补 Bar/Gauge | ⏳ **PR #69 待审** | 分支 `feat/order62-echarts-gauge-bar` @ `62d3fa9`；主 chunk 703.50 kB / gzip **235.62 kB**（改前 675.19/226.16）；`tsc` 0 错误、drawer e2e 8/8、真实浏览器像素级验证 line/bar/gauge 三图出图 |
| 前端告警行增量渲染 | ✅ **已合入** | `AlertLogTable.tsx` / `FleetOverviewCards.tsx` memo 化；`tsc` 0 错误 |
| 可观测性（Prometheus/Grafana） | ✅ **已接线并实跑验证** | 后端 `:8011/metrics` → 200 `text/plain; version=0.0.4`；告警 `:8012/metrics` → 200，实测 `robot_alert_emitted_total{rule_id="motor_temp"} 3.0`、`robot_alert_suppressed_total{rule_id="battery_low"} 2.0`、`robot_alert_robots_online 97.0` |
| 13 可观测性接线 + 6 分支收敛 | ✅ **已回收验证** | 产出 `feat/order-13` @ `911b667`；其 alert `main.py`/`config.py` 因基线过旧判为回退已弃；主代理补上 NPC 漏掉的 `alerts_suppressed` 计数 |
| MQTT 计数器接线 | ✅ **已回收验证** | `metrics.py:41,46` 定义了 `mqtt_received`/`mqtt_dropped` 却从未被引用（恒为 0）；喂坏 payload 实测 `robot_backend_mqtt_dropped_total` 由 0 → 1.0 |
| 19 `robot_alert` 身份 + ack 字段 | ✅ **已回收并反向验证** | 产出 `auto/alert-identity-759f` @ `d27b972`；ORM DDL 与 `init.sql` 逐字同为 `PRIMARY KEY (time, alert_id)`；测试 25 → **29 passed**；注入缺陷实测会红 |
| 上游参考源下载 | ✅ **22/22 齐备**（此前只有 14） | `external/fetch-sources.sh` 声明 22 个，磁盘实存 22 个，缺失/多余均为 0；`external/` 现 **123 MB**。此前「已下载 22 个」的说法**是错的** |
| 15 认证与 RBAC | ⏳ **CNB 执行中** | issue **#36** / `cnb-e2k-1k4jf5tfo`，基线 `main@58bafb5`，workMode=true 已核验。派前复验修正 4 处过期表述 |
| 26 前端健壮性（I-20） | ✅ **已交付，待回收** | 见 issue **#47**。`react-error-boundary@6.1.6` 包 App 外层 + `@tanstack/react-query@5.104.1` 管 Drawer `/history` 缓存/重试；**不替换 zustand**（query 管服务端、zustand 管 WS 客户端状态）。实证：`tsc --noEmit` 0 错误、`vite build` 成功、新增 e2e 7/7 通过 |

本地 HEAD = CNB `main` = **`58bafb5`**（已推送同步）。
远端 **22 个分支**已全部对账完毕（第二轮），实际有价值的独有贡献仅 5 处，均已落地。

> **第二轮对账结论**：用 `git diff main...cnb/$b`（三点）逐支取证后，
> 22 个分支里真正的新东西只有 —— ①MQTT 计数器接线 ②`.gitignore` 4 条
> ③`external/` 第五批重扫 ④`orders/15-18` 换号留档 ⑤`fix/issue19-p0-gaps` 的
> `alert_id` schema 方案（**尚未采用，已整理为订单 19 派给 CNB**）。
> 其余分支均为旧基线的重复或回退版本，见 SOP §6.5 台账。

---

## 三、整顿后的执行计划

### 阶段 A：收口（进行中，CNB 承担）

| 序 | 任务 | 承担方 | 判据 |
|---|---|---|---|
| A1 | 订单 06 端到端联调 + 压测报告 | CNB | `test_integration.sh` 可重复运行；`robot_telemetry` ≈ 30,000 行（真实 SQL 计数）；7 分栏报告 |
| A2 | 订单 07 aiomqtt 契约对齐 | CNB | `aiomqtt==2.5.1`；`Client` 实例复用；退避保留；关闭顺序修正 |
| A3 | 订单 08 asyncpg 升级 | CNB | `asyncpg==0.32.0`；贴出 `import app.main` 真实输出 |

> ⚠️ A2 与 A3 都改 `requirements.txt`。已在 `orders/08-dispatch.md` §6 约定：
> 冲突时以**同时包含两项**为准（`aiomqtt==2.5.1` + `asyncpg==0.32.0`）。

### 阶段 B：主代理回收与验收（A 完成后，我做）

| 序 | 任务 | 说明 |
|---|---|---|
| B1 | **按 SOP §1 步骤 7 回收分支** | `ls-remote --heads` 查分支 → fetch → merge。**不能只看 `status: success`** |
| B2 | 合并后回归验证 | `tsc --noEmit` + `vite build`；后端 `import app.main` |
| B3 | 冲突处理 | 07/08 同改 `requirements.txt`，按 A 表约定合并 |

### 阶段 C：后续增强（待派单）

| 序 | 任务 | 优先级 | 依据 |
|---|---|---|---|
| C1 | ~~前端 bundle 优化（ECharts 按需引入）~~ **已完成** | — | 订单 09 落地（→ gzip 224.90 kB）；Issue #62 补 Bar/Gauge 注册 → gzip 235.62 kB，PR #69 待审 |
| C2 | 告警抑制（inhibition） | 中 | `INTEGRATION-PLAN.md` I-5：离线(severity 3) 时不应同时报低电量(severity 2) |
| C3 | Prometheus 指标接入（后端 `/metrics`） | 中 | 运维层已铺好（PR #26）；`client_python` 已下载。**缺口 G-1/G-2 的收口动作** |
| C4 | GitHub 同步 | 低 | 目前只推了 CNB，**`origin` 尚未同步** |

### 阶段 D：非功能面缺口（PR #26 已铺路，待逐个收口）

> 完整论证与 file:line 见 `external/INTEGRATION-PLAN.md` §7 与 PR #26。

| 序 | 缺口 | 现状 | 收口动作 |
|---|---|---|---|
| G-1 | 全仓无 `/metrics` | 仅 `/healthz` 裸 JSON（`backend_ingestion/app/main.py:66-75`）；`alert_engine` 连 HTTP 都没有 | 引入 `prometheus/client_python` 挂 `/metrics` |
| G-2 | 1000 msg/s 无历史曲线 | `/healthz` 全是瞬时值 | 同上，改成 Counter/Gauge |
| G-3 | 「看板假的绿」无人识别 | 无客户端视角探针 | `mqtt-probe` 已铺；WS 合成探针已落地（`robot_backend_ws_probe_alive` / `robot_backend_ws_probe_failures_total`，ISSUE #58） |
| G-4 | 告警落库失败静默 | `alert_engine/app/store.py:53` 只 `log.exception` | 用 postgres_exporter 指标 + Prometheus 规则 |
| G-5 | 零备份（≈8.64M 行/天） | `infra/ops/pgbackrest/` 只有配置契约 | 换带 pgbackrest 的镜像或起边车 |
| G-6 | `robot_metadata` 的 `last_seen`/`is_online` 从没人写 | `infra/init.sql:39-40` 建了列 | 与 `offline` 规则耦合，单独一单 |
| G-7 | 无多机器人任务级仿真 | `mock_fleet.py` 只产波形 | `AgentVerse` 已下载（未验证能否驱动 100 agent） |

---

## 四、阻塞与已解除项

### 仍存在的阻塞（需账号层处理）

| # | 阻塞 | 证据 | 影响 |
|---|---|---|---|
| B1 | AI 积分耗尽 | `402 [EXCEEDING_LIMIT] AI 积分已用尽` | NPC 无法继续；需根组织管理员提配额 |
| B2 | NPC 不直接推 `main` | 实测 `workMode: true` 时 NPC 能写仓，但产出落在**分支**上 | 这是**设计而非故障**：主代理按 SOP §6.5 对账后按路径回收，避免旧基线分支回退 main |

### 已解除（本轮实证纠正了之前的误判）

| 原判为「环境阻塞」 | 实际根因 | 解决 |
|---|---|---|
| ❌ asyncpg 在 Python 3.13 编译失败 | **我们锁的 `0.29.0` 太旧**（2023-11）；`v0.30.0`(2024-10) 起官方支持 3.13 | 升 `0.32.0` → 已派订单 08 |
| ❌ PyPI 403 装不了依赖 | `files.pythonhosted.org` 被拦，但**镜像源可用** | `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple`（实测 29s 装完） |
| ❌ 派单未带 `--work-mode` | 已修正 | 后续派单均带，核验 `workMode: true` |

---

## 五、验证边界（诚实标注，不得当作已通过）

### 已实测验证

| 项 | 结果 |
|---|---|
| 前端类型检查 | ✅ `tsc --noEmit` 0 错误 |
| 前端生产构建 | ✅ `vite build` 成功（638 modules，58s） |
| 前后端 24 关节键名 | ✅ `telemetry.py:13-22` ↔ `types/robot.ts:8-17` **逐字一致** |
| WS 契约 | ✅ `/ws/robots` + `snapshot/telemetry/alert` 三分支一致 |
| aiomqtt 2.5.1 兼容性 | ✅ `app.main` 导入成功；`Client.__init__` 参数实测 `['self','hostname','port','username']` |
| EMQX 镜像同源 | ✅ 实证：`library/emqx` 由 `emqx/emqx-docker` 构建（official-images `GitRepo` 字段），与 `emqx/emqx` 同源；此前「`emqx:5.6` 非 EMQX」的结论**已撤稿**（对比粒度错误） |
| Schema 契约（init.sql ↔ ORM） | ✅ `tests/test_schema_contract.py` **4/4 通过**；并做过**反向验证**（注入历史缺陷后测试确实 FAIL） |
| `test_integration.sh` health 解析器 | ✅ 8 个用例实测通过（含旧版数组式输出、垃圾输入、字段缺失） |
| 全仓 pytest（`pytest.ini` 生效后） | ✅ **25 passed** —— `tests/test_schema_contract.py` 4 + `alert_engine` 11 + 10。**此前 21 是 alert_engine 局部口径，根 `tests/` 的 4 条从未被跑过** |
| 两服务 `/metrics` 端点 | ✅ 后端 `:8011` 与告警 `:8012` 均返回 200，`content_type=text/plain; version=0.0.4` |
| `.gitignore` 覆盖 `.venv-ci` | ✅ `git check-ignore .venv-ci/pyvenv.cfg` → 命中 `.gitignore:14:.venv*/` |
| 假覆盖测试文件 | ✅ 全仓扫描 **0 个**（`test_*.py` 均含真实 `test_*` 函数） |
| 前端错误边界 + react-query（订单 26） | ✅ `.e2e/run_order26.mjs` **7/7 通过**：子组件抛错被 ErrorBoundary 兜住（不白屏+重试按钮）、`/history` 500 自动重试至成功（后端命中 3 次）、staleTime 内重复查询命中缓存不再打后端 |

本机验证环境（已打通）：Python **3.13.14** 隔离 venv，
`aiomqtt 2.5.1 / asyncpg 0.32.0 / fastapi 0.143.0 / redis 8.1.0 / pydantic 2.14.0`。

### 仍未验证

| 项 | 所需条件 |
|---|---|
| 端到端 30,000 行落库 | **harness 已就绪**（ISSUE #60：`tests/e2e/docker-compose.e2e.yml` + `tests/e2e/test_e2e_30k.py`），但**尚未在真实 Docker 上跑过** —— 本开发环境无 docker daemon，脚本按设计 skip（退出码 3）。需在提供 docker 的机器/runner 上执行 `python -m pytest tests/e2e/test_e2e_30k.py -v -s` 才产生实测数字。CI 侧 `e2e-30k-landing`（`.cnb.yml`）已按实跑报错修正：显式钉 `docker.image: python:3.11-slim`（缺省镜像无 python3，首次实跑返回码 127）、补 `docker compose` 插件探测、并给压测加发送量下限（见 `tests/e2e/README.md`） |
| I-2 / I-3 的**运行时**行为 | 需真实 broker；导入期已验证 |
| 告警去重幂等 / `robot:alerts` 发布 | 同上 |

---

## 六、本轮修掉的真实缺陷（有 file:line 证据）

| # | 缺陷 | 位置 | 后果 |
|---|---|---|---|
| 1 | ~~EMQX 镜像名缺 org 前缀~~ **（撤稿）** | `infra/docker-compose.yml` | 原判据（跨发布通道比 digest）不成立；`emqx:5.6` 与 `emqx/emqx:5.6` 都是 EMQX 5.6.1。本轮改为修正 healthcheck `start_period` 缺失 |
| 2 | `/history` 不返回 `joint_positions` | `backend_ingestion/app/main.py` | Drawer 第三条折线「角度(rad)」**恒空** |
| 3 | Redis Hash 值被字符串化 | `backend_ingestion/app/services/state_cache.py` | 前端收到 `status_code:"1"`，状态色全错 |
| 4 | 批量写入用 flush 时刻 `now()` | `backend_ingestion/app/services/batch_writer.py` | PK `(time,robot_id)` 冲突 → `ON CONFLICT DO NOTHING` **静默丢行** |
| 5 | 过热注入被 clamp 上限压回 | `simulator/mock_fleet.py` | 永远到不了 82°C（实测 max=65.6°C） |
| 6 | **写路径单行坏数据连坐整批** | `backend_ingestion/app/services/batch_writer.py` | 1 条脏 payload 让整批 500 条遥测一起消失（实测 10 好 + 1 坏 → 落库 0 行） |
| 7 | **COPY 路径无幂等** | 同上 | `copy_records_to_table` 不支持 `ON CONFLICT`，重试即抛 `UniqueViolationError` |
| 8 | **ORM 与库内类型漂移** | `backend_ingestion/app/db/models.py` | ORM 编出 `JSON`，`init.sql` 是 `JSONB` → 元数据漂移、丢 JSONB 操作符能力 |
| 9 | 无保留策略，磁盘只增不减 | `infra/init.sql` | 8.64M 行/天，30 天数百 GB；现已加 30d/90d retention |
| 10 | `/healthz` 不反映 DB 可达性 | `backend_ingestion/app/main.py` | DB 挂了仍报 `ok`，值班看不到；现返回 `db: up/down` + `retries`/`rejected` |
| 11 | 告警写入用 `pool.execute()` | `alert_engine/app/store.py` | 坏连接可能被放回池中复用 → 连环失败；改 `acquire()` |

另：`npm install` 曾把 `package-lock.json` 的 42 行 `libc`/`musl` 平台标记删掉
（会导致 Linux CI 上 lock 不完整）—— 已恢复原始跨平台版本。

### 第二轮细节复核（2026-10-10，本轮）

用户反馈「细节完全没有达标」，遂做了一轮**逐文件对照审计**（不依赖记忆，全程实际执行）。
结果分两类：**自己此前写错的结论**，和**一直存在的契约漂移**。

| # | 问题 | 位置 | 性质 | 处置 |
|---|---|---|---|---|
| 6 | 「`emqx:5.6` 是非 EMQX 镜像」**结论本身错了** | `external/SOURCES.md` §1 / `STATUS` / `orders/06` | **撤稿**（拿子 manifest digest 比 index digest，粒度错误） | 重写证据链，见 `external/SOURCES.md` §1.1 |
| 7 | `robot_alert` 的 ORM 凭空多出复合主键 `(time, robot_id)` | `backend_ingestion/app/db/models.py:52` | **真契约漂移**（init.sql 无此 PK） | 已对齐 + 加回归测试 |
| 8 | `test_integration.sh` 用 `docker exec` 判就绪，**绕开 compose healthcheck** | `test_integration.sh` | 判据与被测对象不一致 | 改读 `docker compose ps` 真实健康态 |
| 9 | EMQX healthcheck 缺 `start_period` | `infra/docker-compose.yml` | R-07 §1 强调过的假失败风险 | 补 `start_period: 60s` |
| 10 | 审计脚本自身把 `robot_metadata` 主键报成不一致 | 审计脚本 | **工具不可信** | 修脚本后复验：三表**列名 + 主键全一致** |
| 11 | 仓库**零自动化测试**，R-08 矩阵全靠人工肉眼 | 全仓 | 契约漂移无人拦 | 新增 `tests/test_schema_contract.py`（含**反向验证**） |

> 关于 #6：本轮最重要的不是修了什么，而是承认**上一轮写进文档的「实证」是错的**。
> digest 只能回答「字节是否相同」，不能回答「是不是同一个项目」。
> 跨发布通道比 digest 之前，必须先确认构建来源。

### 第三轮：22 分支全量对账（2026-10-11）

| # | 问题 | 位置 | 性质 | 处置 |
|---|---|---|---|---|
| 12 | `mqtt_received` / `mqtt_dropped` **定义了却从未递增** | `backend_ingestion/app/metrics.py:41,46` ↔ `mqtt_subscriber.py` | 「模块存在但未接线」，两个 Counter **恒为 0** | 补 3 行接线；实测喂坏 payload 后 `dropped_total` 0 → 1.0 |
| 13 | `test_db_integration.py` **收集 0 个用例却静默通过** | `backend_ingestion/tests/` | 假覆盖（断言在 `t1..t6` 里，由 `main()` 驱动） | 改名 `verify_db_integration.py` + 修正 README；全仓扫描现为 0 个假覆盖文件 |
| 14 | 仓库**无 pytest 配置** | 全仓 | 根跑 `pytest` 会收集 `external/`（58 MB 第三方源码），实测 **61 个 collection error 并中断** | 新增 `pytest.ini`（`testpaths` + `norecursedirs`）→ 25 passed |
| 15 | `.gitignore` 挡不住 `.venv-ci` | `.gitignore:10` | 只写 `.venv/`，`fix/issue19-p0-gaps` 因此**整包误提交虚拟环境** | 补 `.venv*/`；`check-ignore` 实测命中 |
| 16 | `robot_alert` 主键靠 `__mapper_args__` 权宜之计 | `backend_ingestion/app/db/models.py:78-86` | ORM 身份 `(time, robot_id)` 与「同 tick 可命中多条规则」**语义冲突**；且 ORM DDL 与 `init.sql` 永远不一致 | 已由订单 19 落地为真实主键 `(time, alert_id)` |
| 17 | 「外部源已下载 22 个」**是错的** | `external/` | `external/*` 被 `.gitignore` 排除、源码不入库，该结论**在仓库里根本无法核对** | 对照 `fetch-sources.sh` 实测：声明 22 / 实存 **14**，缺 8 个；已补齐至 **22/22**（123 MB） |

---

## 七、文档索引

| 文件 | 内容 |
|---|---|
| **本文件** | 主计划与状态 |
| `docs/CNB-DISPATCH-SOP.md` | **如何给 CNB 派单**（含判别真完成的流程） |
| `external/INTEGRATION-PLAN.md` | 上游开源源 → 本项目的套用点（I-1~I-12） |
| `external/SOURCES.md` | **22 个**下载源的 provenance 与取舍理由（第四批见 §5、第五批从零重扫见 **§6**） |
| `external/INTEGRATION-PLAN.md` | 套用点 I-1~I-12；第五批 I-11~I-20 见 **§8** |
| `orders/NN-dispatch.md` | 各单派工正文（最新：**`orders/19-dispatch.md`** `robot_alert` 主键收敛） |
| `pytest.ini` | 把 `external/` 排除出测试收集；`testpaths` 明确为本仓三处测试目录 |
| `docs/requirements/R-*.md` | 验收规格（R-01 前端 / R-02 WS 节流 / R-03 API 契约 / R-07 联调压测 / R-08 验收矩阵） |
| `tests/` | 可重复运行的契约测试（`test_schema_contract.py`：init.sql ↔ ORM 逐列比对，纯标准库） |
| `AGENTS.md` | NPC 每次运行自动注入的工程纪律 |
| `.cnb.yml` / `.cnb/settings.yml` | CI 流水线 / 10 个 NPC 角色 |
