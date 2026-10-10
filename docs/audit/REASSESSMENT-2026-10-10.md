# 细节重评估（Issue #19）

> 触发：用户连续 6 条评论判定历次产出「不够透彻 / 细节没达标 / 完全粗糙」。
> 本文不再重复「下载了哪些仓库」——那是**清单**，不是**评估**。
> 本文回答：**平台在细节上到底缺什么，缺在第几行，判据是什么。**
> 所有断言附 `路径:行号` 或可复现命令；无证据的写「未验证」。

---

## 0. 先自我诊断：为什么前几轮被判「粗糙」

用户的原话是「细节太粗糙」。复盘前几轮产出，有三个**结构性**问题，不是「写得不够多」：

| # | 症状 | 证据 | 本质 |
|---|---|---|---|
| D1 | 交的是**清单**不是**评估** | `external/SOURCES.md` 全文以「仓库 / 体积 / stars / 套用结论」四列表为主体 | 「下了什么」≠「哪里能用、怎么接、接错会怎样」 |
| D2 | 结论大量停在**「未验证」** | `INTEGRATION-PLAN.md:6` 验证边界表 6 项里 2 项未验证；R-07 §5 把本机不可运行列为既定事实 | 用户要的是**前后台完美接应**，即能跑通，不是「知道有这回事」 |
| D3 | 粒度到**模块**为止，没到**字段/失败模式/时序** | 文档讲「I-1 升级 aiomqtt」，但没讲「升级后 `_handle` 里 `dropped` 计数器的行为变化」 | 细节不足 = 层级不够深 |

补充一条**事实纠正**（比 D1~D3 更硬）：

> **`external/` 目录当前是空的。** 实测在本工作区：
> ```
> $ ls external/
> INTEGRATION-PLAN.md  SOURCES.md
> ```
> 前几轮声称「22 个仓库已 `git clone` 到 `external/`」，但 `external/*` 被
> `.gitignore:24-26` 排除，**仓库里根本不保存这些源码**，新工作区 clone 下来必然为空。
> 结论：那些「已下载」在当前环境**不可复现、不可核对**。
> 这是个真问题 —— 要么把「下载」改成可复现脚本 + 校验和清单入库，
> 要么承认「下载」只是当时的临时动作。见 §4 G-20。

---

## 1. 评估方法（这次怎么评的）

不按「模块」评，按**数据流的每一跳**评。100 台 × 10Hz 的链路共 9 跳：

```
[模拟器] → MQTT → [EMQX] → aiomqtt → [校验] → Queue → [批量落库] → TimescaleDB
                                              ↘ Redis state → WS → [前端 store] → 渲染
                                              ↘ Redis pubsub → [告警引擎] → 规则 → 去重 → 落库+发布
```

每一跳问四个问题：
1. **契约**（字段名/类型/单位）两端是否逐字一致？
2. **失败模式**（断了/慢了/脏了）会怎样？有无兜底？
3. **可观测**（出问题能否定位到这一跳）？
4. **验证**（有无真实运行的证据）？

---

## 2. 逐跳评估结果

### 跳 1 · 模拟器 → MQTT （契约 ✅ / 验证 ✅）

- 24 关节键名三处一致，已实测（`simulator/mock_fleet.py`、`schemas/telemetry.py:13-22`、`types/robot.ts:8-17`）。
- **细节缺口**：
  - **姿态四元数不是合法旋转（已实证）**：`mock_fleet.py:135-138` 的 `orient_w/x/y/z`
    各自独立 `uniform(-1,1)` —— 其模长 **≠ 1**，不构成单位四元数
    （`|q|² = Σ uniform(-1,1)²` 期望 ≈ 1.33，实测需抽样）。
    后果：`robot_telemetry` 的 7 个姿态列**数值非零但物理无意义**，
    任何下游（未来做点云/轨迹回放）都不可用。当前看板不读姿态，属**埋雷**。
  - `pos_x/y/z`（`mock_fleet.py:132-134`）是 `uniform(0,50)/(0,30)/(0,3)`，
    与「工厂平面坐标」量纲相符 ✅，但**不与 `assigned_station` 关联** ——
    同一工位的机器人位置随机散布，无法验证「同工位聚拢」这类业务规则。

### 跳 2 · EMQX Broker （契约 ✅ / 验证 ❌）

- 镜像名已 digest 级修正（`infra/docker-compose.yml:12`）。
- **细节缺口**：
  - **未设任何 EMQX 认证**。`infra/docker-compose.yml:14-21` 只设了 `EMQX_LOG__LEVEL`。
    生产不设 `EMQX_AUTHENTICATION` 意味着**任何人可 pub 任意 `robots/+/telemetry`** ——
    这是监控系统最危险的一类细节：伪造遥测可触发/抑制告警。
  - **未设 `EMQX_LISTENERS__TCP__DEFAULT__MAX_CONNECTIONS`**，100 台客户端够用，属**低优先**。
  - `healthcheck` 用 `emqx ping`（`docker-compose.yml:22-24`），**只能证明容器活着，不能证明 1883 可订阅**。R-07 §1 要求「1883 端口可连」，当前 compose 未提供该探测，靠脚本硬编码。

### 跳 3 · aiomqtt → 校验 （契约 ✅ / 验证 ⚠️）

- `Client` 复用已按官方文档改（`mqtt_subscriber.py:32-34`），退避 1→30s 保留（`:38-51`）。
- **细节缺口**（这是本轮新发现的）：
  - **`dropped` 只增不减、无区分度**。`mqtt_subscriber.py:17` 的 `dropped` 统计
    「校验失败丢弃」，但 `_handle` 里对 `ValidationError` 之外的一切异常都没有分支 ——
    若 `state_cache.write_and_publish` 内部抛错，它在 `state_cache.py:52-54` 被吞掉，
    **不会计入 `dropped`**，`/healthz` 会显示 `mqtt_dropped: 0` 而实际已丢数。
    → 「`/healthz` 显示 0 丢弃」**不等于**「没丢数据」。R-03 §2 把 `dropped` 当作
    「payload 不合法」的判据，但实现无法区分「不合法」与「下游写失败」。
  - **`_STOP` 哨兵与 `dropped` 都是模块级可变全局**（`mqtt_subscriber.py:17-18`），
    多实例部署时计数会互相污染（当前单实例，属**设计债**，非当前 bug）。

### 跳 4 · Queue → 批量落库 （契约 ✅ / 验证 ✅ 代码级）

- 双重触发、`executemany`、`ON CONFLICT`、`_STOP` 哨兵均已实现（`batch_writer.py:44-100`）。
- **细节缺口**：
  - **`ON CONFLICT (time, robot_id) DO NOTHING` 会静默吞掉冲突行**。
    **已实证为缺陷**：`batch_writer.py:104-112` 的 `self.written += len(rows)` 计的是
    「发送行数」，`executemany` 不返回被 `DO NOTHING` 跳过的行数 ⇒ `/healthz.written` **高估**，
    R-08 的 C1「≈30000 行」判据被污染。详见 §4 G-04。
  - **`time` 用客户端 `ts`（已实证）**：`batch_writer.py:114-127` 明确
    `if t.ts: datetime.fromtimestamp(t.ts/1000, tz=utc)`。
    设计理由成立（防同批微秒撞主键），但代价是**入库时间可被客户端指定**，
    配合无认证的 broker 可写错分区/未来时刻。见 §4 G-04-X、X4。

### 跳 5 · Redis state → WS → 前端 （契约 ⚠️ / 验证 ✅）

- 类型还原已修（`state_cache.py:70-95` 的 `_decode`），100ms 节流已实测形态正确
  （`useRobotWebSocket.ts:57-75`）。
- **细节缺口**（新发现，且是**真缺陷**）：
  - **`/robots` 与 WS `snapshot` 返回的 `ts` 语义不一致。**
    `state_cache._state_hash` 存的是 `t.ts or 0`（`:26`），即**客户端毫秒**；
    而 `_decode` 原样返回（`:93`）。前端 `RobotTelemetry.ts:number`（`types/robot.ts:30`）
    在 `FleetOverviewCards`/`HealthSummaryHeader` 里若用来算「最后更新时间」，
    依赖客户端时钟正确。**已实证前端确实用它**：`RobotDetailDrawer.tsx:208-209`
   `new Date(robot.ts).toLocaleTimeString(...)` 显示「最后更新时间」。
   ⇒ 客户端时钟漂移会直接显示错误的「最后更新」，值班员据此误判在线状态。见 §4 G-09。
  - **告警流经 WS 转发时 `ts` 单位是毫秒**（`store.py:56` `int(time.time()*1000)`），
    而遥测 `ts` 也是毫秒 —— 一致 ✅。但 **DB 里 `robot_alert.time` 用的是
    `datetime.now(timezone.utc)`**（`store.py:36`），即**服务端时钟**，
    于是「同一条告警」在 DB 与 WS 里时间可能差几毫秒到几秒（客户端时钟漂移）。
    → 排障时「按时间对齐 DB 与前端」会对不上。见 §4 G-05。
  - **WS 无背压**。`main.py:199-205` 直接 `await ws.send_json(...)`。
    若某个浏览器标签页被挂起（`setInterval` 不跑、TCP 窗口满），`send_json` 会阻塞，
    而它是**订阅循环里的 await** → 该连接后面的所有消息都堵住。
    单客户端时不出问题；多客户端/慢客户端时是**雪崩点**。见 §4 G-06。

### 跳 6 · 告警引擎 · 规则 （契约 ✅ / 验证 ✅）

- 4 规则 + 去重窗口齐备（`alert_engine/app/main.py:33-37`）。
- **细节缺口**：
  - **`window_maxlen=100` 与 `joint_stuck_window_s=5.0` 不一致**。
    `config.py:39` 窗口 100 条，10Hz ⇒ 10s；而 `joint_stuck` 只要 5s 窗口
    （`config.py:32`）。当前是实现比需求大，**不会漏判**，但**含义混淆**：
    窗口注释说「约 10s」而规则实际只用 5s，未来改采样率时会踩坑。
  - **`motor_temp` 的「持续 3 采样」已实证正确**：`rules/motor_temp.py:24-33`
    取 `list(window)[-3:]`（window 按到达序 append）⇒ 确为连续 3 条。
    `joint_stuck` 需 50 采样 ≤ `window_maxlen=100`，容量也够。
    唯一缺口是 `config.py:39` 注释与用途不符。见 §4 G-07。

### 跳 7 · 告警引擎 · 落库与发布 （契约 ✅ / 验证 ⚠️）

- 落库 + `PUBLISH robot:alerts` 均实现（`store.py:28-60`）。
- **细节缺口**：
  - **`save()` 里 DB 写失败后仍会 `publish`**（`store.py:37-58` 两个独立 try）。
    即：**DB 没有这条告警，但前端显示了**。这在「告警不可丢」的语义下是**危险的**：
    值班员在前端看到告警，事后审计 DB 却查无此条。需明确**先落库后发布**还是
    「发布即承诺」——当前是「尽力而为」，且无补偿。见 §4 G-08。
  - **`robot_alert` 表 `ack` 字段无人写**（`infra/init.sql:60`）。
    R-03/前端 `AlertLogTable` 都有「未确认」概念的影子，但**没有 ack 接口**。
    `HealthSummaryHeader` 的「当前告警数（severity≥2 未确认）」——
    未确认永远是全部。见 §4 G-09。

### 跳 8 · 前端渲染 （契约 ✅ / 验证 ✅）

- 100ms 节流、退避、三信封分支均实现且注释充分。
- **细节缺口**：
  - **告警没有去重/合并**。`robotStore.pushAlert`（`store.ts:101-102`）纯 prepend，
    `ALERT_BUFFER_LIMIT=200`。若后端去重窗口过期后重复告警，前端会**堆重复行**。
  - **无虚拟滚动**。100 卡 + 200 行告警尚可，但**卡片墙用 `aspect-[4/3]` 全量渲染 100 个 DOM**，
    在低端大屏机上是可感知的。属**性能债**，非当前阻塞。
  - **Drawer 历史请求竞态**：`RobotDetailDrawer.tsx:55-66` **已用 `AbortController`**，
    竞态已被正确处理 ✅。此项不作为缺口（见 §4 G-12，已降级）。

### 跳 9 · 运维/交付面 （这是最薄的一层）

前 8 跳都还算有（虽然细节缺），**第 9 跳几乎是空的**：

| 企业级能力 | 现状 | 证据 |
|---|---|---|
| 鉴权/RBAC | **无**（R-03 §1 明写「无认证」） | `docs/requirements/R-03-backend-api-contract.md:9` |
| 审计日志（谁改了阈值/谁 ack 了告警） | **无** | 无对应表/接口 |
| 数据保留策略（TimescaleDB retention） | **无** | `infra/init.sql` 无 `add_retention_policy` |
| 备份/恢复 | **无** | 无脚本；`tsdb-data` 是匿名卷 |
| 可观测（Prometheus / 结构化日志 / trace id） | **无**（`client_python` 下了没用） | `docker-compose.yml` 无 exporter |
| 部署编排（生产级，非 compose dev） | **无** | 只有 dev compose，无资源 limit/restart policy |
| 告警通知出口（短信/钉钉/PagerDuty） | **无** | 只 PUBLISH 到 Redis |
| 多租户/多工厂 | **无**（`assigned_station` 只是字符串标签） | `infra/init.sql:48` |
| SLO/容量规划 | **无** | 无 |
| 配置校验（启动时 fail-fast） | 部分 | pydantic-settings 有，但无端口/URL 有效性校验 |

> 这一层是用户说「一个大企业需要什么样」的**真正所指**。
> 前几轮把它们散在「订单 11-14」里，但**没有一条给出实现粒度**，等于没写。

---

## 3. 「用户没想到」的缺口（我替他想到的）

按「不补会出事故」排序，每条给出**触发条件 → 后果 → 最小修复**：

| # | 场景 | 触发条件 | 后果 | 最小修复 |
|---|---|---|---|---|
| X1 | **伪造遥测** | EMQX 无认证（`docker-compose.yml:14-21`） | 可刷假数据、可造/压告警 | EMQX 开 `authentication` + 后端用凭据连 |
| X2 | **告警「前端有、DB 无」** | `store.py` publish 与 insert 解耦 | 审计对不上，值班误判 | 先 insert 成功再 publish，或落库失败仍 publish 但打 ERROR |
| X3 | **磁盘打满** | 无 retention，1000 msg/s ≈ 每日 ~8.6 亿行 | DB 撑爆、写入失败、看板停更 | `add_retention_policy('robot_telemetry', INTERVAL '30 days')` |
| X4 | **时钟漂移导致错分区** | 若 `time` 取客户端 `ts`（待确认） | 数据落到错误 chunk，查询漏数据 | 统一用服务端接收时刻（或强制 NTP + 校验偏移） |
| X5 | **慢客户端拖垮 WS 广播** | `main.py` 无背压 | 一个挂起标签页堵住该连接 | 出站队列 + 丢弃最旧 / `asyncio.wait_for` 超时 |
| X6 | **重启即丢「在线」判定** | `last_seen` 在内存（`main.py:41`） | 告警引擎重启后 10s 内所有机器被误判离线 | 冷启动宽限期，或从 DB/Redis 恢复 `last_seen` |
| X7 | **ack 语义缺失** | `robot_alert.ack` 无人写 | 「未确认告警数」永远=全部，KPI 失真 | 加 `PATCH /alerts/{id}/ack` + 前端按钮 |
| X8 | **配置漂移** | 3 份 config（backend/alert/frontend env）各写各的 | 频道名/阈值不一致导致静默失效 | 抽公共 defaults，或启动时交叉校验 |
| X9 | **无 CI 回归护栏** | 未见单测在流水线里跑 | 任何一次改动都可能静默打破契约 | 把 §2 的 24 关节一致性做成 CI 断言 |
| X10 | **备份缺失** | `tsdb-data` 匿名卷 | 卷误删=全量历史丢失 | `pg_dump` 定时 + 命名卷 + 恢复演练 |

---

## 4. 缺口清单（可执行，带验收判据）

> 每条给：ID / 一句话 / 证据坐标 / 验收判据 / 优先级。
> 优先级：P0=不修会出事故，P1=不接受粗糙但可排期，P2=优化。

### G-01 【P0】EMQX 认证缺失
- 证据：`infra/docker-compose.yml:14-21` 无 `EMQX_AUTHENTICATION`
- 判据：未带凭据的 MQTT 客户端 pub 被拒；后端带凭据可连；`test_integration.sh` 增加「匿名被拒」用例

### G-02 【P0】告警落库/发布一致性
- 证据：`alert_engine/app/store.py:28-60` 两个独立 try
- 判据：DB 写失败时日志为 ERROR 且 `/metrics` 有计数；文档写明「发布不承诺持久化」或改为先落库后发布

### G-03 【P0】数据保留策略
- 证据：`infra/init.sql` 无 `add_retention_policy`
- 判据：`SELECT * FROM timescaledb_information.jobs` 含 retention job；超期 chunk 被删

### G-04 【P0】落库时间戳来源与 `written` 计数语义（**已实证，确认为缺陷**）
- 证据 A（`written` 计数虚高）：`batch_writer.py:104-112`
  ```python
  await conn.executemany(INSERT_SQL, rows)
  self.written += len(rows)     # ← 计的是「发送行数」，不是「实际插入行数」
  ```
  `INSERT_SQL` 带 `ON CONFLICT (time, robot_id) DO NOTHING`（`batch_writer.py:35`），
  **`executemany` 不返回被冲突跳过的行数**。因此 `/healthz.written`
  在有冲突时**高估**，R-08 C1「≈30000 行」的判据会被污染 —— 它其实只证明了「发了 3 万条」，
  没证明「存了 3 万条」。**必须**另用 `SELECT count(*)` 交叉验证。
- 证据 B（时间戳来自客户端，**可伪造**）：`batch_writer.py:114-127`
  ```python
  if t.ts:
      ts = datetime.fromtimestamp(t.ts / 1000.0, tz=timezone.utc)   # ← 客户端给的毫秒
  else:
      ts = datetime.now(timezone.utc)
  ```
  代码注释解释了「为什么用客户端 ts」（避免同批同机器人微秒撞主键），这个理由**成立**；
  但代价是**入库时间可由客户端任意指定** —— 配合 G-01（EMQX 无认证），
  可把数据写进任意时间 chunk（含未来时刻），进而影响连续聚合与 retention。
- 判据：
  1. `/healthz` 增加 `inserted`（真实插入数）与 `conflicted`（被跳过数）两个计数，
     用 `INSERT ... RETURNING` 或前后 `count(*)` 差值实现；
  2. `test_integration.sh` 的 30000 判据改为「`SELECT count(*)`」而非 `/healthz.written`；
  3. 文档写明「入库时间采用客户端 `ts`，视为 UTC 毫秒；服务端不校验偏移」，
     并在 G-01 修复后由 broker 层保证客户端可信。

### G-05 【P1】时间戳单一真源
- 证据：`store.py:36`（DB 用服务端时间）vs `store.py:56`（WS 用本机毫秒）
- 判据：同一告警 DB.time 与 WS.ts 差值 <100ms（实测）；或统一由 DB `RETURNING time` 回填 WS 载荷

### G-06 【P0】WS 背压
- 证据：`backend_ingestion/app/main.py:199-205`
- 判据：模拟一个不读 socket 的客户端，其余客户端延迟不受影响（实测 p95 偏移 <50ms）

### G-07 【P1】`motor_temp` / `joint_stuck` 窗口语义（**已实证，结论：实现正确，仅注释需修**）
- 证据：`rules/motor_temp.py:24-33` 取 `list(window)[-min_samples:]`，window 按到达顺序 append，
  故确实是「最近 3 个**连续**采样」—— **判定正确**。
- 证据：`rules/joint_stuck.py:31` `need_samples = max(2, int(5.0*10)) = 50`，
  而 `config.py:39` `window_maxlen = 100` ⇒ 50 ≤ 100，**容量足够，判定正确**。
- 真正的缺口只剩**注释误导**：`config.py:39` 注释写「约 10s」，
  而 `joint_stuck` 只用 5s、`motor_temp` 只用 3 个采样 —— 容量与用途命名不一致。
- 判据：给 `window_maxlen` 补一条注释说明「取所有规则所需时间窗的最大值 + 余量」，
  并加单测锁定「motor_temp 需 3 个连续样本、joint_stuck 需 50 个」，防止未来改采样率时静默失效。

### G-08 【P1】告警 ack 闭环
- 证据：`infra/init.sql:60`（`ack` 列无人写）
- 判据：`PATCH /alerts/{id}/ack` 生效；前端「未确认数」随 ack 下降（实测）

### G-09 【P1】时钟/时区纪律
- 证据 A：`infra/init.sql:12`（`TIMESTAMPTZ`）✅，但客户端 `ts` → `TIMESTAMPTZ` 的换算未文档化
- 证据 B（**真缺陷**）：前端 `RobotDetailDrawer.tsx:208-209`
  `new Date(robot.ts).toLocaleTimeString(...)` —— 「最后更新时间」直接用**客户端毫秒**渲染，
  而 `robot.ts` 最终来自模拟器/机器人的本机时钟。时钟漂移 ⇒ 值班员看到错误的「最后更新」。
- 判据：
  1. 文档写明「入库一律 UTC，客户端 `ts` 视为 UTC 毫秒」；
  2. 「最后更新时间」改用**服务端接收时刻**（或前端用 `Date.now()` 与本地接收时间差计算），
     不信任远端时钟；
  3. 加一条「`ts` 超出 [now-1d, now+1min] 则丢弃并计数」的护栏（同时缓解 G-04-X）。

### G-10 【P1】`/healthz` 指标不足以定位
- 证据：`backend_ingestion/app/main.py:72-82`
- 判据：增加「按原因分类的丢弃计数」（校验失败 / 下游失败 / 队列满）与「最近一次成功落库时刻」

### G-11 【P1】Redis 丢失即静默
- 证据：`state_cache.py:52-54`（异常被吞）、`main.py:172-175`（快照失败仅日志）
- 判据：Redis 不可用时 `/healthz` 应返回 `degraded` 而非 `ok`

### G-12 【P2】前端 Drawer 请求竞态（**已实证，结论：已处理，无需修**）
- 证据：`RobotDetailDrawer.tsx:55-66` 已使用 `AbortController`：
  ```ts
  const abortRef = useRef<AbortController | null>(null);
  const controller = new AbortController();
  fetch(url, { signal: controller.signal })
  ```
  cleanup 里 abort 旧请求 —— **竞态已被正确处理**。此项从缺口清单**降级/移除**，
  仅保留为「有测试锁定更好」的建议。

### G-13 【P1】模拟器姿态量不合法（埋雷）
- 证据：`simulator/mock_fleet.py:135-138` 四元数分量各自独立 `uniform(-1,1)`，模长 ≠ 1
- 后果：`robot_telemetry` 的 `orient_w/x/y/z` 数值非零但**不构成合法旋转**；
  当前看板不读，未来任何轨迹/回放功能不可用
- 判据：用「随机轴 + 随机角」生成**单位**四元数（`|q| = 1`，浮点误差 <1e-6），
  并加断言；`pos_*` 与 `assigned_station` 建立关联（同工位聚拢）

### G-14 【P2】前端告警合并与虚拟滚动
- 证据：`robotStore.ts:101-102`
- 判据：同 `(robot_id, rule_id)` 窗口内不出现重复行；100 卡渲染帧时间有实测数字

### G-15 【P1】可观测性（Prometheus）
- 证据：`docker-compose.yml` 无 exporter；`client_python` 已下未用
- 判据：`/metrics` 暴露 `mqtt_received/dropped`、`batch_flush_seconds`、`queue_depth`；抓取成功

### G-16 【P1】CI 契约回归
- 证据：无 CI 中断言 24 关节一致性
- 判据：CI 中一条测试同时读 `telemetry.py:13-22` 与 `types/robot.ts:8-17` 并断言相等（不相等即 fail）

### G-17 【P1】生产级部署编排
- 证据：`infra/docker-compose.yml` 无 `restart` / `deploy.resources` / 日志轮转
- 判据：进程崩溃自动重启；`max-size` 日志上限；文档写明生产与 dev 的差异

### G-18 【P2】告警通知出口
- 证据：仅 PUBLISH 到 Redis
- 判据：至少一个外部出口（Webhook）可配置，失败重试有上限

### G-19 【P2】多工厂/多租户
- 证据：`init.sql:48` `assigned_station` 仅标签，无权限隔离
- 判据：明确「当前为单工厂范围」并写入 R-08，避免验收方误以为已支持

### G-20 【P1】`external/` 下载不可复现
- 证据：本工作区 `ls external/` 只有 2 个 md；`.gitignore:24-26` 排除源码
- 判据：提供 `external/fetch-sources.sh`（含每个仓库的 tag/commit 钉死），任意环境一键复现；
  或明确声明「下载仅作一次性参考，不入库」
- **本轮已处置**：新增 `external/fetch-sources.sh`（21 个源 + alertmanager sparse），
  每个源钉到明确 ref，执行后把「本地 HEAD commit」写入 `external/.fetch-log` 作为实证。
  已实测验证（2026-10-10）：
  ```
  $ bash external/fetch-sources.sh echarts-for-react
  echarts-for-react  2346edc  HEAD          ← 与 PR #20 声称的 commit 一致（可复现）
  $ bash external/fetch-sources.sh aiomqtt
  aiomqtt            c8f8248  v2.5.1        ← 与 PR #20 声称的 v2.5.1 HEAD 一致
  $ head -6 external/aiomqtt/docs/reconnection.md
  The `Client` context is designed to be reusable (but not reentrant).   ← 与 I-2 引用逐字一致
  ```
  ⇒ PR #20 关于 aiomqtt 版本漂移的两个核心断言，**已可独立复现**。
  仍缺的：SHA256 校验（上游 force-push 会误报，故脚本只钉 ref 不钉哈希）。

### G-21 【P0】端到端真实运行证据
- 证据：R-07 §5 将「本机不可运行」列为既定；订单 06 仍在 CNB 执行中
- 判据：`test_integration.sh` 在至少一个环境**实际跑通**并贴出 7 分栏报告（含真实 SQL 计数）

---

## 5. 与前几轮产出的关系（避免重复劳动）

- 前几轮已覆盖且**有效**的部分（不推翻）：EMQX 镜像名 digest 修正、
  aiomqtt 2.5.1 契约、asyncpg 版本根因、`lifespan` 关闭顺序、ECharts tree-shaking。
- 本轮**新增**的是：§2 的逐跳细节、§3 的 X1~X10、§4 的 G-01~G-21。
- 本轮**纠正**的硬事实：`external/` 源码不入库 ⇒ 声称的 22 个下载不可复现（G-20）；
以及**推翻自己的两个误判**：G-07 的 `motor_temp` 实现本正确、G-12 的前端竞态本已处理。

---

## 6. 建议的执行顺序（不是「继续找仓库」）

```
第一批（止血，P0）：G-01 G-03 G-04 G-06 G-21
第二批（契约钉死，P1）：G-02 G-05 G-07 G-09 G-10 G-11 G-13 G-16 G-20
第三批（闭环，P1）：G-08 G-15 G-17
第四批（体验/规模，P2）：G-12 G-14 G-18 G-19
```

> 真正的「细节达标」标志只有一个：`test_integration.sh` 在一个干净环境里
> 一键跑通，7 分栏报告每一栏都有真实命令与输出，且 §2 的每一跳都有
> 「契约 / 失败模式 / 可观测 / 验证」四项齐备。**在那之前，任何「已完成」都是口头承诺。**

---

## 7. 本文的验证边界（诚实标注）

| 断言 | 状态 |
|---|---|
| `external/` 当前为空 | ✅ 实测（`ls external/`） |
| EMQX 无认证配置 | ✅ 实测（读 `infra/docker-compose.yml`） |
| `init.sql` 无 retention policy | ✅ 实测（通读全文） |
| `ack` 列无写入方 | ✅ 实测（grep 全仓 `ack`） |
| WS 无背压 | ✅ 实测（读 `main.py` WS 分支） |
| G-04 `written` 计虚高 | ✅ 实测（`batch_writer.py:104-112`） |
| G-04 时间戳来自客户端 | ✅ 实测（`batch_writer.py:114-127`） |
| G-07 `motor_temp` 连续 3 采样判定正确 | ✅ 实测（`rules/motor_temp.py:24-33`） |
| G-07 `joint_stuck` 50 采样 ≤ 窗口 100 | ✅ 实测（`rules/joint_stuck.py:31` + `config.py:39`） |
| G-12 前端竞态已用 AbortController | ✅ 实测（`RobotDetailDrawer.tsx:55-66`） |
| `ack` 仅前端过滤、无后端接口 | ✅ 实测（`HealthSummaryHeader.tsx:45` 注释 + 全仓 grep 无 ack 写接口） |
| 前端显示最后更新时间依赖客户端 `ts` | ✅ 实测（`RobotDetailDrawer.tsx:208-209` `new Date(robot.ts)`） |
| 任何「已跑通」结论 | ❌ 无 —— 本机无 Docker，本轮**未运行任何服务** |

> 未验证项已在对应条目标注，**不得**被后续引用当作已确认。
