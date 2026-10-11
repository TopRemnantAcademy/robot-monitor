# tests/load —— 可判定的压测（订单 23 / I-16）

> **一句话**：用 k6 + xk6-mqtt 替换 emqtt-bench 的「只吐数字」，
> 让「连接成功率 / 发布延迟 / WS 收包率」都变成机器可判的 `check()` 断言，
> 直接进 k6 退出码，而不是让人去读一串数字自己判断。

---

## 1. 为什么换掉 emqtt-bench

| | emqtt-bench（Erlang） | 本目录（k6 + xk6-mqtt） |
|---|---|---|
| 输出 | 只吐数字（吞吐、时延） | 数字 **+ `check()` 的 PASS/FAIL** |
| 判定 | 人工看图定阈值 | 阈值写进 `thresholds`，**不达标即退出码非 0** |
| CI 可用性 | 要 OTP 27.2+ 编译 | 官方发布预编译二进制，或 `xk6 build` 一条命令 |
| 断言能力 | 弱 | JS 任意逻辑（字段类型、报文内容、语义） |

依据：`external/INTEGRATION-PLAN.md` §8.6（I-16）、`external/SOURCES.md` §5（N7/N8）。
`R-07 §6.1` 明确「禁止用『日志无报错』代替通过判定」——
emqtt-bench 的数字恰好属于「日志无报错」那一类。

⚠️ **不替换 `simulator/mock_fleet.py`**（订单 23 明确，`INTEGRATION-PLAN.md` §8.6 亦同）。
mock_fleet 是 24 关节真实 payload 的唯一产生者，本目录只做**压测与判定**。

---

## 2. 文件清单

| 文件 | 作用 |
|---|---|
| `payload.js` | 与 `mock_fleet.py` **逐字段同构**的 payload 生成器（24 关节） |
| `config.js` | 压测参数 + 判定阈值（全部可用环境变量覆盖） |
| `mqtt_pub.js` | MQTT 压测主体：N 台 × 10Hz 发布 + `check()` 断言（**需 xk6-mqtt**） |
| `ws_watch.js` | WS 收包率 / snapshot 规模 / 字段类型断言（**官方 k6 即可**） |
| `run.sh` | 一键入口：先决条件 → 压测 → 探针 → 汇总（区分环境错误/判定失败） |
| `test_payload_contract.py` | **单测**：把 `payload.js` 与 `mock_fleet.py` 逐字段对拍 |
| `last-run.json` / `last-ws-run.json` | 最近一次运行的判定摘要（`handleSummary` 产出） |

---

## 3. 拿到「带 xk6-mqtt 的 k6」（三选一）

官方 k6 **不含** `k6/x/mqtt` 模块，直接跑会报 `unknown module: k6/x/mqtt`。

```bash
# A) 预编译产物（最快；注意资产 URL 带版本号，不是 /latest/）
curl -sL https://github.com/grafana/xk6-mqtt/releases/download/v0.3.3/xk6-mqtt_v0.3.3_linux_amd64.tar.gz \
  | tar xz -C /tmp && mv /tmp/xk6-mqtt_v0.3.3_linux_amd64/k6 ./k6-mqtt

# B) 自行编译（需 Go）
go install go.k6.io/xk6/cmd/xk6@latest
xk6 build --with github.com/grafana/xk6-mqtt@latest --output ./k6-mqtt

# C) 复用已有的
K6_BIN=/path/to/k6-mqtt bash tests/load/run.sh
```

实测二进制自报扩展（本机验证）：
```
k6 v2.3.0 (go1.27.1, linux/amd64)
Extensions:
  github.com/grafana/xk6-mqtt v0.3.3, k6/x/mqtt [js]
```

---

## 4. 跑法

```bash
# 完整（MQTT 压测 + WS 探针）—— 需 broker 与后端都起着
bash tests/load/run.sh

# 小规模冒烟（不起 100 台）
ROBOTS=20 DURATION_S=10 bash tests/load/run.sh

# 只跑 MQTT，或只跑 WS
bash tests/load/run.sh --mqtt-only
WS_TOKEN=<jwt> bash tests/load/run.sh --ws-only

# 只探测环境，不起任何负载
bash tests/load/run.sh --preflight-only
```

**退出码**（沿用 `test_integration.sh` 的约定，R-07 §6.3）：

| 码 | 含义 |
|---|---|
| 0 | 全部判定通过 |
| 1 | 存在**判定失败**（阈值/check 红了）—— 代码或配置问题 |
| 2 | **环境不具备**（没有 k6 二进制 / broker 未起 / 后端未起）→ 相关项标「未验证」 |

### 可直接用 k6 跑单个脚本

```bash
./k6-mqtt run tests/load/mqtt_pub.js          # 默认 100 台 × 10Hz × 30s
./k6-mqtt run tests/load/ws_watch.js          # WS 探针（官方 k6 也可）
```

### 关键环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ROBOTS` | `100` | 机器人台数（`spec/00 §1`） |
| `PUBLISH_HZ` | `10` | 每台每秒发布数（`spec/00 §1`） |
| `DURATION_S` | `30` | 稳态时长（`R-07 §2` 用 30s） |
| `RAMP_S` | `5` | 爬坡时长（避免连接风暴误判） |
| `BROKER_URL` | `mqtt://localhost:1883` | xk6-mqtt 接受 `mqtt://` URL |
| `WS_URL` | `ws://localhost:8000/ws/robots` | 后端 WS 端点 |
| `WS_TOKEN` | 空 | JWT。**订单 15 起 WS 需 `?token=`**，留空会被 close 1008 拒绝 |
| `K6_BIN` | `./k6-mqtt` | 带扩展的 k6 路径 |
| `TH_PUB_P95_MS` 等 | 见 `config.js` | 判定阈值覆盖 |

取 JWT（后端起着时）：

```bash
curl -s -X POST http://localhost:8000/auth/jwt/login \
  -d 'username=<email>&password=<pwd>' | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])'
```

---

## 5. 判定的三条硬断言（对应 R-07/R-08）

| # | 断言 | 出处 | 阈值 | 实现位置 |
|---|---|---|---|---|
| ① | **连接成功率** | `R-07 §1` 就绪判据 | ≥ 0.99 | `mqtt_pub.js` 的 `'MQTT 连接已建立'` + `thresholds.checks` |
| ② | **发布延迟** | `R-07 §3` | p95 < 50ms，p99 < 200ms | `mqtt_pub.js` 的 `pub_latency_ms` Trend |
| ③ | **WS 收包率 / 首帧** | `R-08 C4/C5` | snapshot ≥ 100 台；字段为数字率 ≥ 0.99 | `ws_watch.js` 的 `ws_snapshot_robots` / `ws_telemetry_fields_ok` |

**每条都接了 `thresholds`**，不达标 k6 退出码非 0。
这一点是**反向验证过的**（见 §7「断言有效性」）——只写 `check()` 不接阈值，会红着判通过。

### payload 契约怎么保证「复用 mock_fleet 真实 24 关节结构」

`payload.js` 的关节名与字段是从 `simulator/mock_fleet.py` 抄来的，
但「抄」不可信，所以有 `test_payload_contract.py`：

```bash
python tests/load/test_payload_contract.py     # 或 python -m pytest tests/load/
```

它用 `ast` **解析** `mock_fleet.py`（不是正则猜），再**实际用 node 执行** `payload.js`，
逐项断言：24 关节键名与顺序、顶层字段集合、`robot_id` 正则、
三个关节字典键一致、`max_joint_temp == max(joint_temperatures)`、报文大小同量级。

---

## 6. 本机实测证据（2026-10-11）

环境：容器内 Linux amd64；broker 为本地 `aedes`（MQTT 3.1.1）、
WS 服务为按后端契约复刻的 `ws` 服务端（三段信封 + `?token=` 拒绝语义）。
k6 = 官方发布 `xk6-mqtt v0.3.3` 预编译二进制。

### 6.1 满规模：100 台 × 10Hz × 30s

```
[load] broker=mqtt://localhost:1883 robots=100 hz=10 vus=100 duration=30s ramp=5s
       target=1000 msg/s expected=30000 条
[load] 判定摘要（checks rate=1.0000，pass=285 fail=0）
```

| 指标 | 实测 | 目标（R-07 §3） | 判定 |
|---|---|---|---|
| check 通过率 | **285/285 = 1.0000** | — | PASS |
| 发布吞吐 | **35696 条 / 30s = 1190 msg/s** | ≥ 1000 msg/s | PASS |
| 发布延迟 p95 | **1 ms** | < 50 ms | PASS |
| 发布延迟 p99 | **1 ms** | < 200 ms | PASS |
| 发布延迟 max | 12 ms | — | 观测值 |

> `1189.9 msg/s` 略高于 1000 是正常的：稳态窗口外还夹带了爬坡尾段，
> 故用 `EXPECTED_PUBLISHES` 做量级校验、用 `checks` 判每 VU 的节奏达标率。
> **这不是「放宽阈值」**（R-07 §6.2）—— R-07 的 ≥1000 msg/s 判据是下界。

### 6.2 WS 探针

```
[ws] 判定摘要（checks rate=1.0000，pass=84 fail=0，messages=80）
  [PASS] WS 探针：check 通过 84/84（失败率 0% <= 5%）
```

### 6.3 run.sh 全链路

```
==> 汇总
  通过 4 项 / 判定失败 0 项 / 环境错误 0 项 / 未验证 0 项
  —— 通过 ——
     * MQTT 压测
     * k6 thresholds（MQTT）
     * WS 探针
     * k6 thresholds（WS）
  结论：全部通过
```

---

## 7. 断言有效性（**反向验证**，本仓纪律要求）

写测试不算数，要证明「它真的会红」。三组反向验证实际跑过：

| 场景 | 构造 | 结果 |
|---|---|---|
| **坏 WS 服务端** | snapshot 只发 50 台 + `status_code`/`battery_pct` 字符串化 | `checks rate=0.6000（3 pass / 2 fail）`，**k6 退出码 99** |
| **无 token** | `WS_TOKEN=` 留空（订单 15 起会 close 1008） | `setup()` 抛错，**退出码 107**，只打印 1 条说明（不刷屏） |
| **broker 未起** | 停掉 broker 后 `run.sh --mqtt-only` | 归为**环境错误**，**退出码 2**，MQTT 项标「未验证」 |

### 反向验证暴露并修掉的**两个真缺陷**

1. **只写 `check()` 不接 `thresholds` → 红着判通过。**
   首版 `ws_watch.js` 只声明了 `ws_up` / `ws_telemetry_fields_ok` 两条阈值，
   `checks` 没接。实测：在「50 台 + 字符串字段」的坏服务端上，k6 仍打印
   `ws_probe ✓` 并 **以退出码 0 结束**。补上 `checks: ['rate>=…']` 后才变 99。
2. **被拒重连风暴把 summary 冲爆。**
   把 `check()` 放在 `socket.on('error')` / `on('close')` 里，会在 k6 自动重连时
   **反复触发**：实测一次 2s 会话炸出 **43647 条 fail**，通过率被稀释到 0.25，
   摘要完全失去意义。修法：收尾断言只在**首次 close** 结账一次（`settled` 标志），
   且「未带 token」改在 `setup()` 里中止，不进风暴。

---

## 8. 未验证 / 边界（诚实标注）

| 项 | 为什么未验证 | 需要什么才能验证 |
|---|---|---|
| **真实 EMQX broker** | 本机无 Docker，压测用本地 `aedes` 起的 MQTT 3.1.1 broker | `docker compose -f infra/docker-compose.yml up -d` 后重跑，并核对 `emqx ctl` 的连接数/丢弃指标 |
| **真实后端 `/ws/robots`** | 同上，WS 用按契约复刻的替身服务端 | 起 `backend_ingestion` 并取到真 JWT（`WS_TOKEN`） |
| **端到端落库行数 ≈ 30000** | 需 TimescaleDB 真实 SQL 计数 | 这是 `test_integration.sh` 的职责（`R-08 F3`），本目录不重复 |
| **前端渲染 ≈10 次/秒（R-08 E4）** | 100ms 节流是**浏览器内**行为 | 需 Playwright/真浏览器计数；`ws_watch.js` 只判**服务端→客户端投递** |
| **`xk6-mqtt` 与 EMQX 5.6 的协议细节** | 未对真 EMQX 跑过 | 同上起 EMQX 后重跑 |
| **publish 延迟的「真值」** | 本项测的是**写入 socket** 的同步代价（qos=0） | R-07 §3 的 P95<200ms 是**落库批耗时**，口径不同，勿混用 |

⚠️ 另外两点口径提醒：

- **`noConnectionReuse` / 连接模型**：每个 VU 建一条 MQTT 连接。100 VU = 100 连接。
  把 `ROBOTS` 调到上千前，先确认 broker 的 `max_connections`。
- **发布时机语义**：`client.publish()` 是同步版（`mqtt/client_publish.go:22`，
  内部 `token.Wait()`），故 `pub_latency_ms` 是「写入 socket + 等 token 完成」的耗时，
  不含 broker 落盘，也不含后端消费。

---

## 9. 与其他测试的关系（别重复跑）

| 层 | 在哪 | 判什么 |
|---|---|---|
| payload 结构契约 | `tests/load/test_payload_contract.py` | `payload.js` ↔ `mock_fleet.py` 逐字段 |
| **压测判定** | **本目录** | 连接成功率 / 发布延迟 / WS 投递率（`R-07`、`R-08 C1-C5`） |
| 端到端落库 | `test_integration.sh` | SQL 真值计数 ≈ 30000 行（`R-08 F3`） |
| schema 契约 | `tests/test_schema_contract.py` | `init.sql` ↔ ORM |
| 前端行为 | `frontend_dashboard` 的类型/构建检查 | 节流、渲染次数（`R-08 E1-E10`） |
