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

`.cnb.yml` 的 `e2e-30k-landing` pipeline（PR / push main / 每日 crontab 三触发共用同一份 stages）
挂 `services: [docker]`，并**显式钉 `docker.image: python:3.11-slim`**。

为什么必须钉镜像：首次实跑时没写 `image`，缺省镜像 `cnbcool/default-build-env` **没有 python3**，
日志里 docker 探测已经通过，紧接着 `sh: line 21: python3: command not found`（返回码 127）——
job 变红但和代码毫无关系。脚本里也做了两层探测（docker → python），
任一层缺失都打印【E2E SKIP】+ 原因 + 解法并以 0 退出，绝不把环境问题伪装成回归。

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

## 与 `test_integration.sh`（订单 06）的关系

`test_integration.sh` 在**宿主机裸跑**且**无 Docker 就整体退出**，没有 CI 分支。
本目录是它的容器化补充：入口（模拟器）也进容器，栈可一键重建，
并显式区分「环境不具备 → skip」与「断言失败 → red」。两者不替代对方：
前者是人工联调脚本，后者是可进 CI 的用例。
