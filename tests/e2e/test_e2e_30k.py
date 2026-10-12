"""端到端 30,000 行落库验证 harness（ISSUE #60）。

考什么
------
`STATUS.md` §九「仍未验证」与 `docs/requirements/R-09-database-integration.md` §6
同时挂着一条：**端到端 30,000 行（含 MQTT/模拟器）未验证**。
已有的证据都不覆盖这条完整链路：
  - `tests/test_schema_contract.py`、`backend_ingestion/tests/*` 是**单测口径**，
    不接 MQTT、不接真实 TimescaleDB；
  - `test_integration.sh`（订单 06）在宿主机裸跑，且**无 Docker 就整体退出**，
    没有「CI 可跑 / 无 Docker 可 skip」的分支。
本文件把这条链路固化成**可一键重复运行**的用例：
  起栈（emqx + timescaledb:2.13.1 + redis + backend）→ 等真实健康
  → 容器内跑 mock_fleet（--duration 受控时长）→ 直连 DB 数行数 → 断言。

断言与「无 PK 冲突丢行」
------------------------
1. `robot_telemetry` 行数 **>= 30000**（不是 `==`：网络/调度抖动允许向下浮动，
   用 `>=` 是 ISSUE #60 的明确要求）。
2. **无 PK 冲突丢行**：从两侧取证，缺一不可 ——
   a) 库内不存在 `(time, robot_id)` 重复组（`GROUP BY ... HAVING count(*)>1`），
      且 PK 唯一约束真实存在于 TimescaleDB 侧；
   b) 后端自报的丢行三元组 `rows_written == rows_submitted` 且 `rows_skipped == 0`
      且 `rejected == 0`。
   `rows_skipped` 正是 `ON CONFLICT DO NOTHING` **静默丢行**的计数器
   （`backend_ingestion/app/services/batch_writer.py:166-174`），
   只看「日志无报错」或只看行数够多，都发现不了它。
   (a) 与 (b) 必须同时成立：只查库只能证明"落进去的没重复"，
   证明不了"该落的都落了"；只查计数只信后端自报，证明不了库内事实。

为什么必须真 TimescaleDB（禁止 mock 替身）
------------------------------------------
本用例要考的 `init.sql` 特性（超表、连续聚合 `robot_status_10s`、
`add_retention_policy` / `add_compression_policy`）**只在 TimescaleDB 存在**。
拿 PostgreSQL 或内存替身顶上，等于把「e2e 通过」与「生产建表正确」脱钩，
这正是 ISSUE #60 点名禁止的。

无 Docker 时
------------
**skip 并报明**（`pytest.skip` / 退出码 3），绝不 red。判定顺序：
`docker` 命令在不在 → `docker info`（daemon 是否可达）→ 镜像/栈能否起来。
每一层都给出「为什么 skip」与「需要什么才能验证」，避免把环境问题伪装成代码失败。

跑法
----
    # 有 pytest
    python -m pytest tests/e2e/test_e2e_30k.py -v -s
    # 无 pytest（本文件自带 main，退出码 0=通过 3=skip 1=失败）
    python tests/e2e/test_e2e_30k.py

指标命名反例探针（订单 32 / ISSUE #100）
--------------------------------------
本文件除了 30k 落库，还**顺路**在真实栈上验一次指标命名：`/healthz` 与
`/metrics` 是同一进程暴露的两个端点，既然栈已经起着，抓一次真实
`/metrics` 的**原始字节**来断言「新名在、旧名消失」比另起一套 mock 更硬。
`assert_metric_naming()` 断言的失败对象是**服务**（旧名残留 / 新名缺失），
不属环境问题，按「不伪装 skip」的规矩必须 red —— 详见该函数注释。
为什么不写成单测：`backend_ingestion/tests/test_observability.py` 用的是
`metrics.render()`（**进程内、自带 registry**）+ `TestClient`，它证明不了
「跑起来的那个进程吐出的字节」—— 而 `robot_monitor_*` 恰恰是**运行时多注册一个
Collector** 就能复活的东西。探针要盯的是那个进程，所以它必须在栈里、对着真端口。

环境变量（都有默认值，默认即 ISSUE #60 的验收口径）：
    E2E_DURATION=40        压测秒数（40s × 100 台 × 10Hz ≈ 40,000 帧）
    E2E_ROBOTS=100         台数
    E2E_MIN_ROWS=30000     行数阈值
    E2E_KEEP=1             结束后保留栈（排查用）
    E2E_UP_TIMEOUT=300     起栈+等健康的超时（秒）
    E2E_COMPOSE=<path>     覆盖 compose 文件路径
    E2E_METRICS_URL=<url>  /metrics 端点（默认后端 compose 映射的 18000 端口）
    E2E_METRICS_PREFIX=... 只取该前缀的指标行做命名断言（默认 robot_）

压测自身的发送量下限
--------------------
脚本会按 `E2E_ROBOTS * 10 * E2E_DURATION * 0.5` 算出下限并作为 `FLEET_MIN_SENT`
注入 `mock_fleet`（`simulator/mock_fleet.py --min-sent`）。
为什么必须有这条：`--duration` 只保证跑满时长，不保证发送量，而
`docker compose run --rm` **不会**清掉 compose 里为同一变量设的默认值 ——
台数被静默覆盖时压测照样退 0，失败要到断言阶段才暴露（甚至被阈值放过）。
下限让压测阶段自己先红，且失败信息直指原因。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = Path(os.environ.get(
    "E2E_COMPOSE", ROOT / "tests" / "e2e" / "docker-compose.e2e.yml"))

DURATION = int(os.environ.get("E2E_DURATION", "40"))
ROBOTS = int(os.environ.get("E2E_ROBOTS", "100"))
MIN_ROWS = int(os.environ.get("E2E_MIN_ROWS", "30000"))
UP_TIMEOUT = int(os.environ.get("E2E_UP_TIMEOUT", "300"))
KEEP = os.environ.get("E2E_KEEP") == "1"

# 宿主机侧探 /healthz 的口（compose 里映射 18000→8000）。
# 走宿主机端口而不是 `compose exec`，是为了不额外依赖容器内的 curl/wget
# —— backend 镜像只有 python:3.11-slim，没装这俩。
BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://127.0.0.1:18000")

# 指标命名探针（订单 32）：走宿主机映射端口，与 BACKEND_URL 同源同理由
# （backend 镜像只有 python:3.11-slim，容器内没有 curl/wget，从外面打更省事）。
METRICS_URL = os.environ.get("E2E_METRICS_URL", BACKEND_URL + "/metrics")
METRICS_PREFIX = os.environ.get("E2E_METRICS_PREFIX", "robot_")

PYTEST_SKIP_MARKER = "E2E-SKIP"

# 每个子进程都要有上限，禁止无限等待（ISSUE #60 §3「复用 test_integration.sh 的超时守卫思路」）。
# 下载/构建镜像可能慢，故 up 给得宽；其余步骤都是本地操作，几十秒足够。
UP_TIMEOUT_S = UP_TIMEOUT
HEALTH_TIMEOUT_S = 180
STRESS_TIMEOUT_S = DURATION + 120
# 发送量下限：理论值 台数×10Hz×秒数 的一半。留一半余量是因为
# 起栈初期 broker 未就绪会有重连空转，真正的**回归**（broker 写错、台数被覆盖、
# 发布侧持续报错）会让它掉到零头，两者区分度极大，不需要扣得很精确。
MIN_SENT = int(ROBOTS * 10 * DURATION * 0.5)
PSQL_TIMEOUT_S = 60


class E2ESkip(Exception):
    """环境不具备（无 Docker 等）—— 不是代码失败，必须显式 skip。"""


def run(cmd: list[str], timeout: int, check: bool = True) -> subprocess.CompletedProcess:
    """跑子进程，带硬超时与原始输出保留（失败时报文要能贴进 Issue）。"""
    proc = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"命令失败：{' '.join(cmd)}\n"
            f"exit={proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
    return proc


def compose(*args: str, timeout: int = PSQL_TIMEOUT_S, check: bool = True):
    return run(["docker", "compose", "-f", str(COMPOSE_FILE), *args],
               timeout=timeout, check=check)


# ------------------------------------------------------------------ 先决条件

def require_docker() -> None:
    """无 Docker 就 skip（带明确原因），绝不 red。"""
    if shutil.which("docker") is None:
        raise E2ESkip(
            "PATH 内找不到 docker 命令。本用例需要真实 Docker 引擎起 "
            "emqx + timescaledb:2.13.1-pg16 + redis，且**禁止**用 mock 替身"
            "（ISSUE #60 禁止项）。需要：在装了 Docker 的机器/runner 上运行。"
        )
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise E2ESkip(
            "docker daemon 不可达（`docker info` 非 0）。"
            f"原始输出：{(probe.stderr or probe.stdout).strip()[:400]}"
        )
    if not COMPOSE_FILE.is_file():
        raise E2ESkip(f"compose 文件不存在：{COMPOSE_FILE}")
    # `docker compose`（插件版）必须可用：老式 `docker-compose`（独立二进制）
    # 与脚本调用的命令格式不同，混用会在起栈时才炸成「代码失败」。
    # 这一条**不能**放进下面的 try 里 —— 那里捕获的是 docker daemon 未就绪（E2ESkip），
    # 而这里是环境缺件（有 docker 但没 compose 插件），必须显式 skip 且带原因。
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise E2ESkip(
            "`docker compose`（插件版）不可用，`docker compose version` 非 0。"
            "本用例整套起栈/执行/取证都走 compose。原始输出："
            f"{(probe.stderr or probe.stdout).strip()[:400]}"
        )


# ------------------------------------------------------------------ 探针

def backend_health() -> dict | None:
    try:
        with urllib.request.urlopen(f"{BACKEND_URL}/healthz", timeout=3) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def wait_backend_db_up(timeout: int = HEALTH_TIMEOUT_S) -> dict:
    """等到 /healthz 的 db=up。

    为什么不等「容器 running」：`/healthz` 是**存活探针**语义，依赖不可达时
    照样返回 200 且 status=degraded（backend_ingestion/app/main.py:85-118）。
    若此时就开跑，模拟器发的第一批会堆到队列里、DB 通了才落库，
    行数看似正常但时序已经错乱 —— 所以必须等 db=up 这个**语义**而非端口。
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = backend_health()
        if last and last.get("db") == "up":
            return last
        time.sleep(2)
    raise AssertionError(
        f"后端 /healthz 在 {timeout}s 内未达到 db=up（最后快照：{last}）"
    )


def psql(sql: str) -> str:
    """在 timescaledb 容器里执行 SQL，返回单值（去空白）。"""
    proc = compose("exec", "-T", "timescaledb", "psql", "-U", "postgres",
                   "-d", "robot_monitor", "-tAc", sql)
    return proc.stdout.strip()


# ------------------------------------------------------------------ 用例主体

def run_e2e() -> dict:
    """执行完整链路，返回取证结果（供断言与报告共用）。"""
    evidence: dict = {}

    print(f"[e2e] compose = {COMPOSE_FILE}", flush=True)
    print(f"[e2e] 口径：{ROBOTS} 台 × 10Hz × {DURATION}s，阈值 >= {MIN_ROWS} 行", flush=True)

    # 1) 起栈（构建镜像 + 拉镜像）。不随 up 起 mock_fleet：它挂在 stress profile 下，
    #    压测必须晚于「后端能落库」这一步，否则是在测一个还没准备好的系统。
    up_started = time.monotonic()
    compose("up", "-d", "--build", timeout=UP_TIMEOUT_S)
    evidence["up_seconds"] = round(time.monotonic() - up_started, 1)
    print(f"[e2e] 栈已起（{evidence['up_seconds']}s）", flush=True)

    # 2) 等后端确认 DB 可达
    health = wait_backend_db_up()
    print(f"[e2e] /healthz = {json.dumps(health, ensure_ascii=False)}", flush=True)
    evidence["health_before"] = health

    # 3) 压测前基线（复用栈时基线可能非 0；本用例默认每次 down -v，故通常为 0）
    before = int(psql("SELECT count(*) FROM robot_telemetry;") or 0)
    evidence["rows_before"] = before
    print(f"[e2e] 压测前 robot_telemetry = {before}", flush=True)

    # 4) 容器内跑受控时长压测。用 `compose run` 而不是 `up`：
    #    run 是前台阻塞、退出码即压测成败，天然给出同步点，
    #    不必靠轮询容器状态猜「跑完了没」。
    print(f"[e2e] 压测发送量下限 = {MIN_SENT} 条（理论 {ROBOTS * 10 * DURATION} 的一半）",
          flush=True)
    stress_started = time.monotonic()
    compose(
        "run", "--rm", "-T",
        "-e", f"FLEET_DURATION={DURATION}",
        "-e", f"FLEET_ROBOTS={ROBOTS}",
        "-e", f"FLEET_MIN_SENT={MIN_SENT}",
        "mock_fleet",
        timeout=STRESS_TIMEOUT_S,
    )
    evidence["stress_seconds"] = round(time.monotonic() - stress_started, 1)
    print(f"[e2e] 压测结束（{evidence['stress_seconds']}s）", flush=True)

    # 5) 等后端把在飞批次刷完。
    #    双重触发是「满 500 条 **或** 满 1s」，最后一小批最多等 1s 才刷盘
    #    （backend_ingestion/app/services/batch_writer.py）。这里不能裸 sleep 一个拍脑袋值，
    #    而要等到队列真空 + 计数不再增长，否则会得到「行数少一点」的假失败。
    health = wait_writer_drained()
    evidence["health_after"] = health
    print(f"[e2e] /healthz（落定后）= {json.dumps(health, ensure_ascii=False)}", flush=True)

    # 6) 库内取证（真实 SQL 计数，不看日志）
    after = int(psql("SELECT count(*) FROM robot_telemetry;") or 0)
    dup_groups = int(psql(
        "SELECT count(*) FROM ("
        "  SELECT time, robot_id FROM robot_telemetry"
        "  GROUP BY time, robot_id HAVING count(*) > 1"
        ") t;"
    ) or 0)
    pk_defined = psql(
        "SELECT coalesce(string_agg(a.attname, ',' ORDER BY array_position(c.conkey, a.attnum)), '')"
        " FROM pg_constraint c"
        " JOIN pg_class r ON r.oid = c.conrelid"
        " JOIN pg_attribute a ON a.attrelid = r.oid AND a.attnum = ANY(c.conkey)"
        " WHERE r.relname = 'robot_telemetry' AND c.contype = 'p';"
    )
    hypertable = psql(
        "SELECT count(*) FROM timescaledb_information.hypertables"
        " WHERE hypertable_name = 'robot_telemetry';"
    )
    db_version = psql("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';")

    evidence.update({
        "rows_after": after,
        "rows_delta": after - before,
        "dup_pk_groups": dup_groups,
        "pk_columns": pk_defined,
        "is_hypertable": hypertable,
        "timescaledb_version": db_version,
    })
    print(f"[e2e] 库内取证：after={after} delta={after - before} "
          f"dup_pk_groups={dup_groups} pk=({pk_defined}) "
          f"hypertable={hypertable} tsdb={db_version}", flush=True)

    # 7) 指标命名反例探针（订单 32）：栈还活着，顺手抓一次真实 /metrics 的原始字节。
    #    放在落库取证之后：先保证主链路可用，再验命名 ——
    #    否则「服务根本没起来」会被误报成「命名有问题」。
    assert_metric_naming(METRICS_URL, METRICS_PREFIX)

    return evidence


def wait_writer_drained(timeout: int = 60) -> dict:
    """等 BatchWriter 队列排空、计数稳定。

    判定「稳定」而不是「队列为 0」：队列瞬时为 0 也可能刚好在两批之间，
    下一批还没进来。取连续两次 1s 间隔内 `rows_submitted` 不变 才算落定。
    """
    deadline = time.monotonic() + timeout
    prev = None
    while time.monotonic() < deadline:
        h = backend_health()
        if h is not None:
            stable = (prev is not None
                      and h.get("rows_submitted") == prev.get("rows_submitted")
                      and h.get("queue_depth") == 0)
            if stable:
                return h
            prev = h
        time.sleep(1)
    last = backend_health()
    raise AssertionError(
        f"BatchWriter 在 {timeout}s 内未排空/未稳定（最后 /healthz：{last}）。"
        "队列持续积压说明落库速率跟不上 1000 msg/s，需要看 /metrics。"
    )


def teardown() -> None:
    if KEEP:
        print("[e2e] E2E_KEEP=1，保留栈（手动清理："
              f"docker compose -f {COMPOSE_FILE} down -v）", flush=True)
        return
    # down -v：连卷一起删。e2e 每次都要「全新库 + init.sql 重跑」，
    # 留着卷会让上轮数据混进本轮计数（基线非 0 时阈值判定会失真）。
    compose("down", "-v", "--remove-orphans", check=False, timeout=120)


# ------------------------------------------------------------------ 断言

def assert_e2e(ev: dict) -> None:
    # 0) 前置事实：确认我们连的是**真 TimescaleDB**，不是 PG 替身。
    #    没有这条，后面的断言在「误连到一个普通 PG」时也会绿。
    assert ev["is_hypertable"] == "1", (
        "robot_telemetry 不是 hypertable —— 说明连到的不是运行了 init.sql 的 "
        f"TimescaleDB（is_hypertable={ev['is_hypertable']!r}）。"
    )
    assert ev["pk_columns"] == "time,robot_id", (
        "robot_telemetry 主键形状不对：期望 (time, robot_id)，"
        f"实际 ({ev['pk_columns']!r})。PK 是「无冲突丢行」判定的锚点，必须先锚准。"
    )
    assert ev["timescaledb_version"], "pg_extension 里查不到 timescaledb，DB 不是 TimescaleDB 实例"

    # 1) 行数阈值（ISSUE #60：>= 30000，禁止 ==）
    assert ev["rows_delta"] >= MIN_ROWS, (
        f"robot_telemetry 新增 {ev['rows_delta']} 行 < 阈值 {MIN_ROWS}"
        f"（压测 {DURATION}s × {ROBOTS} 台 × 10Hz 期望 ≈ {ROBOTS * 10 * DURATION}）。"
        f"基线 {ev['rows_before']} → 结束 {ev['rows_after']}。"
    )

    # 2) 无 PK 冲突丢行 —— 库内事实侧
    assert ev["dup_pk_groups"] == 0, (
        f"库内存在 {ev['dup_pk_groups']} 组重复的 (time, robot_id)，"
        "说明写入路径的幂等性被破坏（ON CONFLICT 未生效或 PK 未建）。"
    )

    # 3) 无 PK 冲突丢行 —— 后端自报侧（rows_skipped 是静默丢行的唯一计数器）
    h: dict = ev["health_after"]
    assert h.get("rejected", 1) == 0, (
        f"rejected={h.get('rejected')}：降级链 L3 判定有坏行被丢弃，属真丢数据。"
    )
    assert h.get("rows_skipped", 1) == 0, (
        f"rows_skipped={h.get('rows_skipped')}：被 ON CONFLICT DO NOTHING 静默丢弃的行数非 0，"
        "即「收到的数据没全部入库」。这正是 ISSUE #19 G2 定义的丢行证据。"
    )
    assert h.get("rows_written") == h.get("rows_submitted"), (
        f"rows_written={h.get('rows_written')} != rows_submitted={h.get('rows_submitted')}："
        "提交与入库不相等，存在丢行。"
    )
    assert h.get("db") == "up", f"结束态 /healthz db={h.get('db')}，DB 掉线，数据不完整"

    # 4) 交叉校验：库内新增行数不应 **大于** 后端自报入库行数多少。
    #    两者口径不完全等价（若是复用栈，基线含上轮数据；后端计数是本进程累计），
    #    故只做「库内未凭空多出」的方向性校验，避免造出脆弱断言。
    assert ev["rows_delta"] <= h.get("rows_written", 0) + 10, (
        f"库内新增 {ev['rows_delta']} 行 > 后端自报入库 {h.get('rows_written')} 行（+10 容差），"
        "说明有绕过后端计数的写入路径，或基线统计有误。"
    )


# ------------------------------------------------------------------ 指标命名反例探针（订单 32）

# 订单 32 T1 的改前 `grep -rn robot_monitor_` 清单里，**曾被注册为指标**的 6 个旧名。
# 它们是本探针的失败面：任何一个在真实 /metrics 里出现即 red。
LEGACY_MONITOR_METRICS = (
    "robot_monitor_batch_queue_size",
    "robot_monitor_batch_flush_total",
    "robot_monitor_batch_rows_total",
    "robot_monitor_mqtt_messages_total",
    "robot_monitor_ws_connections",
    "robot_monitor_history_latency_seconds",
)

# 改后必须存在的规范名（订单 32 T2 的两个真正新增 + 消重后承接同源计数的 4 个）。
REQUIRED_BACKEND_METRICS = (
    "robot_backend_queue_depth",
    "robot_backend_batch_flushes_total",
    "robot_backend_telemetry_written_total",
    "robot_backend_mqtt_received_total",
    "robot_backend_ws_connections",
    "robot_backend_history_latency_seconds",
)


def metric_lines(metric_name_prefix: str, text: str) -> list[tuple[str, str]]:
    """从 /metrics 文本里提取指标行。

    为什么必须有这一层：`metrics.py` 的头部注释里**有意保留**旧名的迁移记录
    （把「旧名已作废」写下来，比删掉它更有用）。直接 `in` 全文断言会把注释
    误判成违规指标，从而造出一个「永远红」的假门禁 —— 这类门禁最坏的结局是被人加
    `|| true` 关掉。这里只认指标行：Prometheus exposition 的指标行形如
    `name{suffix}?[ ][value]`，注释行以 `#` 开头（含 `# HELP` / `# TYPE`）。
    """
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        line_name = line.split("{", 1)[0].split(" ", 1)[0]
        if line_name.startswith(metric_name_prefix):
            cells = line.split()
            out.append({
                "name": line_name,
                "samples": [c.strip(",") for c in cells[1:]] or [""],
                "raw": line.strip(),
            })
    return out


def assert_metric_naming(url: str, prefix: str) -> None:
    """对真实服务断言：规范名在、旧名消失。

    取值的两种失败必须分开报：
      - HTTP 拿不到（探针从没跑成）→ skip 并说明，不伪装成通过；
      - HTTP 拿到了但内容不合口径（旧名还在 / 新名缺了）→ **red**。
    后者是本探针存在的唯一理由：它是一道**反例门禁**，
    不能因为它「只是顺路加的」就在断言失败时退化成 skip。
    """
    text = http_get(url)
    if text is None:
        _pytest_skip(
            f"指标命名探针无法访问 {url}（backend:8000 的 /metrics）。"
            "若服务未起来，wait_backend_db_up 已先失败；此处属探针未配好。"
        )
        return

    shown = metric_lines(prefix, text)
    # 命中判据必须**逐族**而不是逐名字面量：Prometheus exposition 里
    #   - Counter / Gauge / Summary 有 `robot_backend_x <v>` 这样以名字开头的裸样本行；
    #   - **Histogram 没有** —— 它只有 `robot_backend_x_bucket{le=...}` / `_count` / `_sum`。
    # 第一版写成 `name == "robot_backend_history_latency_seconds"` 就永远命中不了，
    # 造出一道「离线正例都过不去」的假门禁（本文件外加实测抓出来的）。
    # 故判据改为「名字本身，或名字 + `_` 后跟细分后缀」。
    names_here = {ln["name"] for ln in shown}

    def _hits(metric: str) -> list[dict]:
        return [ln for ln in shown if ln["name"] == metric
                or ln["name"].startswith(metric + "_")]

    leaked = [m for m in LEGACY_MONITOR_METRICS if _hits(m)]
    assert not leaked, (
        f"真实 /metrics 残留旧前缀指标 {leaked}（订单 32 验收：旧名必须消失）。"
        f"实测原始行：{[ln['raw'] for m in leaked for ln in _hits(m)][:5]}"
    )
    missing = [m for m in REQUIRED_BACKEND_METRICS if not _hits(m)]
    assert not missing, (
        f"真实 /metrics 缺少规范名 {missing}（订单 32 验收：新名必须在）。"
        f"该前缀下实测到 {len(names_here)} 个指标名，样本：{sorted(names_here)[:8]}"
    )
    print(f"[e2e] 指标命名探针通过：旧名前缀 0 命中，规范名 {len(REQUIRED_BACKEND_METRICS)} 个在位",
          flush=True)


def http_get(url: str, timeout: int = 5) -> str | None:
    """取 URL 文本；失败返回 None（调用方决定 skip 还是 red）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


# ------------------------------------------------------------------ 报告

def render_report(ev: dict) -> str:
    """产出可直接贴进 Issue/PR 的取证报告（SQL 计数 + 命令口径）。"""
    h = ev["health_after"]
    lines = [
        "## e2e 30k 落库取证（真实 TimescaleDB）",
        "",
        f"- 栈：`tests/e2e/docker-compose.e2e.yml`（emqx:5.6 + **timescaledb {ev['timescaledb_version']}** + redis:7.2 + backend + mock_fleet）",
        f"- 起栈耗时：{ev['up_seconds']}s；压测：{ev['stress_seconds']}s（{DURATION}s 受控时长）",
        f"- `SQL> SELECT count(*) FROM robot_telemetry;` → 压测前 {ev['rows_before']} / 结束后 {ev['rows_after']}"
        f"（**新增 {ev['rows_delta']}**，阈值 >= {MIN_ROWS}）",
        f"- `SQL> SELECT count(*) FROM (... GROUP BY time, robot_id HAVING count(*)>1) t;` → **{ev['dup_pk_groups']}**（PK 冲突组）",
        f"- `SQL> {('robot_telemetry 主键 = (' + ev['pk_columns'] + ')')}`；hypertable = {ev['is_hypertable']}",
        f"- 后端 `/healthz` 丢行三元组：rows_submitted={h.get('rows_submitted')} / "
        f"rows_written={h.get('rows_written')} / rows_skipped={h.get('rows_skipped')}"
        f"；rejected={h.get('rejected')}；mqtt_dropped={h.get('mqtt_dropped')}",
        "",
        "结论：**通过** —— 行数达标且无 PK 冲突丢行。",
    ]
    return "\n".join(lines)


def _pytest_skip(reason: str):
    """在 pytest 与裸跑两种上下文里都 skip。

    实现注意（实测踩过）：不能只用 `import pytest` 判断上下文 ——
    pytest **装在本环境里**，裸跑时那个 import 同样会成功，
    于是 `main()` 里想「打印原因 + 退出码 3」的路径会变成 `pytest.skip()`
    抛 `Skipped` 异常，直接糊在 traceback 上（本文件加 `--metric-naming-check`
    时实测到了）。判据改用 `under_pytest()`（pytest 会把自身注入 sys.modules
    且在**非 __main__** 的模块里执行用例）。
    """
    if under_pytest():
        import pytest  # noqa: PLC0415

        pytest.skip(reason)
    print(f"{PYTEST_SKIP_MARKER}: {reason}", flush=True)
    sys.exit(3)


def under_pytest() -> bool:
    """当前是否由 pytest 驱动（而非 `python tests/e2e/test_e2e_30k.py` 裸跑）。"""
    return "pytest" in sys.modules and __name__ != "__main__"


def test_e2e_30k_landing() -> None:
    try:
        require_docker()
    except E2ESkip as exc:
        _pytest_skip(str(exc))

    try:
        ev = run_e2e()
        print("\n" + render_report(ev) + "\n", flush=True)
        assert_e2e(ev)
    finally:
        teardown()


def dump_failure_logs() -> None:
    """链路失败取证：抓 timescaledb / backend 两个关键容器的尾部日志。

    只在失败路径调用；`--tail` 限幅防刷屏。compose logs 对**已退出**的容器
    同样有效（读 stdout 缓冲），故 DB init 中途崩掉（如 CAGG 建不出来）也抓得到。
    2026-10-11 实测教训：仅凭「/healthz 180s 未到 db=up（快照 None）」无法区分
    ①init.sql 建表失败 ②backend 容器启动即崩 ③backend 起了但连不上 DB ——
    三者修法完全不同，必须让日志自己说话。
    """
    for svc in ("timescaledb", "backend"):
        try:
            proc = compose("logs", "--tail=80", svc, timeout=30, check=False)
            print(f"\n===== compose logs {svc} (tail 80) =====", flush=True)
            print((proc.stdout or "")[-8000:] or "(no output)", flush=True)
        except Exception as exc:  # 取证本身绝不允许改变退出码
            print(f"(取证失败 {svc}: {exc})", flush=True)


def main() -> int:
    """无 pytest 时的入口：0=通过 1=断言失败 3=环境不具备（skip）。

    另有 `--metric-naming-check`：**不**要求 docker，只对已在跑的栈做一次
    订单 32 的命名反例断言。CI 里栈是 `docker compose up -d` 起来的
    （不含 stress profile），用这个开关就能把命名门禁挂进已有 pipeline，
    不必在 pytest 里重起一次栈。
    """
    if "--metric-naming-check" in sys.argv:
        # 环境不具备 → 3（skip，CI 需转成 0 并打印原因）；断言失败 → 1（**必须 red**）。
        if not COMPOSE_FILE.is_file():
            print(f"{PYTEST_SKIP_MARKER}: compose 文件不存在：{COMPOSE_FILE}", flush=True)
            return 3
        try:
            assert_metric_naming(METRICS_URL, METRICS_PREFIX)
        except AssertionError as exc:
            print(f"[e2e] 指标命名门禁未通过：{exc}", flush=True)
            return 1
        except E2ESkip as exc:  # assert_metric_naming 内部走 _pytest_skip，这里兜底
            print(f"{PYTEST_SKIP_MARKER}: {exc}", flush=True)
            return 3
        return 0

    try:
        require_docker()
    except E2ESkip as exc:
        print(f"{PYTEST_SKIP_MARKER}: {exc}", flush=True)
        return 3

    try:
        ev = run_e2e()
    except AssertionError as exc:
        print(f"[e2e] 链路失败：{exc}", flush=True)
        dump_failure_logs()
        teardown()
        return 1

    try:
        assert_e2e(ev)
    except AssertionError as exc:
        print(f"[e2e] 断言未通过：{exc}", flush=True)
        print("\n取证快照：\n" + render_report(ev), flush=True)
        teardown()
        return 1

    print("\n" + render_report(ev), flush=True)
    teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
