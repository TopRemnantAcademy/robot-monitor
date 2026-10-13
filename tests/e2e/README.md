# e2e：端到端 30,000 行落库验证（ISSUE #60）

把「100 台 × 10Hz → MQTT → 后端批量落库 → 真实 TimescaleDB ≥ 30,000 行且无 PK 冲突丢行」
固化成一条可重复运行的命令。此前这条链路只在**单测口径**验证过，
`STATUS.md` §九与 `docs/requirements/R-09-database-integration.md` §6 都挂着「仍未验证」。

## 文件

| 文件 | 作用 |
| :--- | :--- |
| `docker-compose.e2e.yml` | 一次性栈：`emqx:5.6` + `timescaledb:2.13.1-pg16` + `redis:7.2` + `backend` + `mock_fleet`（`stress` profile） |
| `Dockerfile.mock_fleet` | 模拟器镜像（`simulator/` 原本只有裸跑，没有 Dockerfile） |
| `test_e2e_30k.py` | 驱动 + 断言 + 取证报告；无 Docker 时 **skip（退出码 3）**，绝不红 |

## CI 里怎么跑

`.cnb.yml` 的 `e2e-30k-landing` pipeline（PR / push main / 每日 crontab 三触发）挂
`services: [docker]`，并**显式钉 `docker.image: python:3.11-slim`**。

为什么必须钉镜像：首次实跑时没写 `image`，缺省镜像 `cnbcool/default-build-env` **没有 python3**，
日志里 docker 探测已经通过，紧接着 `sh: line 21: python3: command not found`（返回码 127）——
job 变红但和代码毫无关系。脚本里也做了两层探测（docker → python），
任一层缺失都打印【E2E SKIP】+ 原因 + 解法并以 0 退出，绝不把环境问题伪装成回归。

### 三处触发必须共用同一份声明（订单 34 归因）

订单 32 把 e2e stage 加长之后，push / pull_request 两块跟着改了，**`"crontab: 0 3 * * *"` 那块没改**：
它当时只写了 `services/env/stages`，**漏了 `docker.image`**。于是同一份 stages 在 crontab 触发下
跑在缺省镜像里，`python` 探测那层直接命中 → 打一句【E2E SKIP】→ `exit 0` → 构建整体绿，
但**一行数据都没落**。同一 sha（`68b7b8b`）的两个构建是对照组：

- push `cnb-152-1k4ohjp3k-002` prepare：`docker pull python:3.11-slim`，e2e stage 打印 `python 可用：Python 3.11.17`
- crontab `cnb-st7-1k4onp4ok-001` prepare：`docker pull cnbcool/default-build-env`，e2e stage 打印【E2E SKIP】

现状：`docker.image` 提成 YAML 锚点 `&e2e-image`，三处 `docker: *e2e-image` 引用同一个值 ——
一致性由 YAML 层保证，不再靠人记得改三遍。

### SKIP 必须打横幅（订单 34 T2）

任何「没实跑」的退出路径都先调用 `skip_banner`，打印固定串：

```
================================================================
【E2E SKIPPED —— 本构建未实跑 30k 落库】
原因：<具体原因>
本构建的 e2e 为绿色但**未产生任何落库数字**，不可当作 ISSUE #60 的证据。
================================================================
```

之前 SKIP 只打一行 `【E2E SKIP】...`，混在几百行日志里与刷屏无异；CI 列表上只看得到「绿」。

### 命名探针原用 `curl`，在 `python:3.11-slim` 里恒不可达（订单 34 T2）

`python:3.11-slim` 是 Debian slim，**不带 curl/wget**。而订单 32 那段探测写的是
`curl -sf http://127.0.0.1:18000/healthz` / `/metrics` —— 于是无论栈起没起都判「不可达」，
**这道门禁从挂上去那天起一次都没真跑过**（`cnb-sfb-1k4oh9va6-002`、`cnb-152-1k4ohjp3k-002`
的 e2e stage 日志里都是「【E2E SKIP】订单 32 命名探针：/metrics 在 180s 内不可达」）。
现改为调用 `$PY -c "import urllib.request; ..."`（本 job 的运行时本体），不引入新依赖。

### 命名探针的两类判据：无 label / 带 label（订单 34）

修完 `curl` 之后，探针第一次真跑就红了：`/metrics` 缺少 `robot_backend_batch_flushes_total`。
**这不是命名回归** —— 该指标在 `backend_ingestion/app/metrics.py:77` 注册得好好的，缺的是**样本行**：
prometheus_client 对带 label 的 Counter **惰性注册**，序列要等第一次 `labels(reason=...).inc()` 才诞生。
而本 stage 是 `up -d` 起完栈就探、不起 `mock_fleet`，backend 从未刷过批。

两种修法都成立，边界不同：

- **探针侧（本单采用）**：把判据拆成 `REQUIRED_BACKEND_METRICS`（无 label，随时应在）与
  `LABELED_BACKEND_METRICS`（有事件才导出）。零流量的独立 `--metric-naming-check` 传
  `allow_unemitted=True`，缺席**打印但不判红**；30k 落库路径（压测之后）保持默认 `False`，
  要求全量在位 —— 对它的要求**不打折**。
- **服务侧（另一修法，本单未采用）**：在 `/metrics` 暴露前对两个恒定枚举 label 各 `inc(0)` 预热，
  让带 label 的 Counter「从第一个 scrape 起」就有 0 值行。这能同时消灭「指标不存在」与
  「指标恒为 0」在面板上无法区分的隐患，**是一次真实的可观测性改进** ——
  但它要改 `backend_ingestion/app/metrics.py`，落在这张单的文件白名单之外
  （白名单：`.cnb.yml` / `tests/e2e/**` / `STATUS.md` / `docs/**`），故不适合塞进本 PR，
  应另开一单（订单 32 的后续）。

两者不冲突：服务侧预热后 `allow_unemitted` 分支不会被触发。本单选探针侧，纯粹是**白名单约束**。

### shell 会在非 0 后终止整段脚本（订单 34 T2）

实测（`cnb-dr0-1k4pbtmr7-002`）：命名探针返回 1 之后，脚本**没有**继续往下跑 30k，
而是直接 `Finished, code: 1, duration: 1m 1s`。即本 stage 的 shell 带 `set -e` 语义，
不是 `set -uo pipefail` 能关掉的。故所有可能非 0 的命令都就地吞退出码
（`cmd || rc=$?`），把判定收拢到脚本末尾显式 `exit`。

## 跑法

```bash
# pytest
python -m pytest tests/e2e/test_e2e_30k.py -v -s

# 或裸跑（0=通过 1=断言失败 3=环境不具备）
python tests/e2e/test_e2e_30k.py
```

手工分步（排查时用）：

```bash
docker compose -f tests/e2e/docker-compose.e2e.yml up -d --build
# 等 /healthz 的 db=up
FLEET_DURATION=40 docker compose -f tests/e2e/docker-compose.e2e.yml \
  --profile stress run --rm mock_fleet
docker compose -f tests/e2e/docker-compose.e2e.yml exec -T timescaledb \
  psql -U postgres -d robot_monitor -tAc "SELECT count(*) FROM robot_telemetry;"
docker compose -f tests/e2e/docker-compose.e2e.yml down -v
```

## 环境变量

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `E2E_DURATION` | `40` | 压测秒数（40s × 100 台 × 10Hz ≈ 40,000 帧） |
| `E2E_ROBOTS` | `100` | 台数 |
| `E2E_MIN_ROWS` | `30000` | 行数阈值（ISSUE #60 口径） |
| `E2E_UP_TIMEOUT` | `300` | 起栈 + 拉/构建镜像的超时（秒） |
| `E2E_KEEP` | — | `1` 则结束后保留栈，便于人工排查 |
| `E2E_COMPOSE` | `tests/e2e/docker-compose.e2e.yml` | 覆盖 compose 文件路径 |

`E2E_ROBOTS` / `E2E_DURATION` 会被换算成 `FLEET_MIN_SENT = 台数 × 10 × 秒数 × 0.5`
注入 `mock_fleet`（见下条）。

## 断言口径（为什么这么写）

1. **行数用 `>=`，不用 `==`**：网络/调度抖动允许向下浮动（ISSUE #60 明令）。
2. **「无 PK 冲突丢行」两侧同时取证**，缺一不可：
   - 库内：`GROUP BY time, robot_id HAVING count(*)>1` 必须为 0，且 PK 形状是 `(time, robot_id)`；
   - 后端：`rows_written == rows_submitted`、`rows_skipped == 0`、`rejected == 0`。
     `rows_skipped` 是 `ON CONFLICT DO NOTHING` **静默丢行**的唯一计数器
     （`backend_ingestion/app/services/batch_writer.py:166-174`）。
   只查库拿不到「该落的没落」，只信自报拿不到「库内事实」。
3. **必须先真 TimescaleDB**：断言里先核 `is_hypertable=1` 与 `timescaledb` 扩展版本，
   防止误连到普通 PostgreSQL 时断言假绿。mock 替身被 ISSUE #60 点名禁止。
4. **等 `/healthz` 的 `db=up` 再开跑**：`/healthz` 是存活探针语义，依赖不可达时仍返回 200，
   只判端口会提前开跑；结束时等到 `rows_submitted` 连续两次不变才算落定（队列排空）。
5. **压测阶段自带发送量下限**：`mock_fleet --min-sent`（由脚本按 `台数×10×秒数×0.5` 注入）。
   只有 `--duration` 时，模拟器**跑满时长就退 0**，发不出去也不吭声：
   `docker compose run --rm` 不会清掉 compose 里为同一变量设的默认值，
   台数被静默覆盖、broker 写错、发布侧持续报错，都会一路苟到断言阶段甚至被阈值放过。
   加了下限，失败发生在**离因最近的地方**，日志直接点名原因。

## 订单 32：指标命名反例探针（`--metric-naming-check`）

`test_e2e_30k.py` 除落库断言外，还对**真实 `/metrics` 的原始字节**断言指标命名：

```bash
# 栈已在跑（含 backend 的 18000→8000 映射）时：
python tests/e2e/test_e2e_30k.py --metric-naming-check
# 0=通过 1=命名不合口径（red） 3=环境不具备（skip）
```

断言两件事：6 个 `robot_monitor_*` 旧名 0 命中、6 个 `robot_backend_*` 规范名在位。
为什么必须是运行时探针而不是单测：`backend_ingestion/tests/test_observability.py`
跑的是 `metrics.render()`（进程内自带 registry）+ `TestClient`，
**它证明不了「跑起来的那个进程吐出的字节」** —— 而旧前缀恰恰是「多注册一个
Collector 就复活」的东西。探针盯的是进程，所以它必须在栈里、对着真端口。

细节（都是实测踩出来的）：

- 命中判据**逐指标族**而非逐名字面量：Histogram 没有 `name <value>` 裸样本行，
  只有 `name_bucket{le=}` / `_count` / `_sum`。第一版按字面量等值匹配，
  连正例都过不去 —— 那会造出一道「永远红」的假门禁。
- 只认**指标行**（跳过 `#` 注释）：`metrics.py` 头注释里有意保留了旧名的迁移记录，
  全文 `in` 断言会把迁移记录误判成违规。
- 探针自身做了反例自检（见 PR 描述）：注入 `robot_monitor_ws_connections` 后必红。

CI 里挂在 `e2e-30k-landing` stage 的**最前面**，自己 `up -d`（不带 stress profile）
拿到活的 backend，断言完再 `down`。HTTP 探不到 → `[SKIP]` 并 exit 0；
断言失败 → 退出码 1 → **stage 变红**（一个吞掉失败的门禁比没有门禁更糟）。

## 实跑记录

### 2026-10-13 — 首次真实落库（订单 34 T3）

构建：PR **#111** 的 pull_request build **`cnb-vro-1k4pcqp07`**（pipeline `-002`，e2e stage **160.8s**，
非 SKIP）。运行环境 `python:3.11-slim`（`python 可用：Python 3.11.17`）+ `services: [docker]`。

```
== e2e 30k 落库验证（ISSUE #60）==
docker 可用：Docker version 29.6.2, build dfc4efb
python 可用：Python 3.11.17
== 订单 32：指标命名反例探针 ==
[e2e] 指标命名探针：带 label 指标尚未导出（本轮零流量，属预期）：['robot_backend_batch_flushes_total']
[e2e] 指标命名探针通过：旧名前缀 0 命中，规范名 5 个在位
订单 32 命名门禁通过：/metrics 旧名 0 命中，robot_backend_* 规范名全部在位。
== 30k 落库主链路（本 job 的硬门槛产出）==
[e2e] compose = /workspace/tests/e2e/docker-compose.e2e.yml
[e2e] 口径：100 台 × 10Hz × 40s，阈值 >= 30000 行
[e2e] 栈已起（9.1s）
[e2e] /healthz = {"status": "ok", "db": "up", ..., "rows_submitted": 0, "rows_written": 0, "rows_skipped": 0}
[e2e] 压测前 robot_telemetry = 0
[e2e] 压测发送量下限 = 20000 条（理论 40000 的一半）
[e2e] 压测结束（52.5s）
[e2e] /healthz（落定后）= {"status": "ok", "db": "up", "written": 39864, "flushes": 80,
      "retries": 0, "rejected": 0, "queue_depth": 0, "mqtt_received": 39864, "mqtt_dropped": 0,
      "rows_submitted": 39864, "rows_written": 39864, "rows_skipped": 0}
[e2e] 库内取证：after=39864 delta=39864 dup_pk_groups=0 pk=(time,robot_id) hypertable=1 tsdb=2.13.1
[e2e] 指标命名探针通过：旧名前缀 0 命中，规范名 6 个在位
e2e stage 全部通过：30k 落库断言通过 + 命名门禁在位。
```

| 指标 | 实测值 | 口径 |
| :--- | :--- | :--- |
| `robot_telemetry` 新增行数 | **39864** | `>= 30000`（ISSUE #60） |
| 起栈 / 压测时长 | 9.1s / 52.5s | 40s 受控时长 |
| PK 冲突组 `GROUP BY time, robot_id HAVING count(*)>1` | **0** | 必须为 0 |
| `rows_submitted` / `rows_written` / `rows_skipped` | 39864 / 39864 / **0** | 三者相等，skipped=0 |
| `rejected` / `mqtt_dropped` | 0 / 0 | 无坏行 |
| hypertable / PK / TimescaleDB | 1 / `(time,robot_id)` / **2.13.1** | 真 TimescaleDB，非 PG 替身 |

**这是 ISSUE #60「端到端 30,000 行落库」的第一份真实数字**（此前 `STATUS.md` §五、
`docs/requirements/R-09-database-integration.md` §6 一直挂着「未验证」）。

## 与 `test_integration.sh`（订单 06）的关系

`test_integration.sh` 在**宿主机裸跑**且**无 Docker 就整体退出**，没有 CI 分支。
本目录是它的容器化补充：入口（模拟器）也进容器，栈可一键重建，
并显式区分「环境不具备 → skip」与「断言失败 → red」。两者不替代对方：
前者是人工联调脚本，后者是可进 CI 的用例。
