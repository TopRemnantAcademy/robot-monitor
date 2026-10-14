"""cagg 实时聚合回填的真库回归测试：`/history` 两条数据源（R-13）+ 看板最新态（R-16）。

考什么
------
三个 cagg 都必须是**实时聚合**（`timescaledb.materialized_only = false`）：

  * R-13 / §5.1c：`robot_history_10s`（`/history` 短跨度，`end_offset=10s`）
    与 `robot_history_1h`（`/history` 长跨度，`end_offset=1h`）；
  * R-16 / §5.1d：`robot_status_10s`（**看板最新态**，`end_offset=1min`）。

若是 TimescaleDB 2.13.1 的默认 `materialized_only = true`，查询视图就只覆盖
`[.., now()-end_offset]`，`[now()-end_offset, now()]` 这段**查不到** ⇒
`/history` 右端空洞（1h / 10s）与「看板最新值滞后约 1min」。本用例把「实时回填生效」
锁成一个**可失败**信号：一旦有人把其中任一个改回 `materialized_only = true`
（或删掉 `infra/init.sql` §5.1c / §5.1d 的 `ALTER ... SET (materialized_only = false)`），
本用例立即红。

两条路径的关系
--------------
R-16 只**追加** `robot_status_10s`：§5.1c 那两条 R-13 的 ALTER 必须**原样保留**
（本用例同时守着它们，回退 R-13 同样变红）。

做法（真 TimescaleDB，禁 mock）
-------------------------------
复用 `tests/e2e/docker-compose.e2e.yml` 的 `timescale/timescaledb:2.13.1-pg16`
（与 `infra/docker-compose.yml` 同版本；ISSUE #60 点名**禁止** mock 替身冒充真实 TimescaleDB）：

  1. 起 timescaledb 容器 → 容器首启自动执行挂载进去的 `infra/init.sql`
     （即被测对象本身，含 R-13 的 2× + R-16 的 1× `ALTER ... SET (materialized_only = false)`）；
  2. 校验三视图的 `materialized_only` 实为 `false`（目录 `_timescaledb_catalog.continuous_agg`），
     且 `pg_get_viewdef` 含 `UNION ALL ... FROM robot_telemetry` 实时段（第二条旁证）；
  3. 向 `robot_telemetry` 灌 `[now()-30min, now()]` 的数据（1800 行 × 1 台，10Hz）；
  4. 断言 `SELECT count(*) FROM robot_history_1h  WHERE bucket >= now()-'90 min' AND robot_id=$1` **> 0**
     **且** `... robot_history_10s ...` **> 0**
     **且** `... robot_status_10s ...` **> 0**（灌进去的数据必须能通过 cagg 查回）。

第三条（R-16）的单测口径
------------------------
`robot_status_10s` 的 `end_offset` 是 **1min**，与另外两个不同，故它的实时区是
`[now-1min, now]`。灌入的 1800 行覆盖 `[now-30s, now]`（= 前 30 秒，10Hz 时 300 行；
整体窗口仍是 `[now-30min, now]`），必然落在该实时区内 —— 这正是本 R-16 要证明的
「看板最新态不再滞后 1min」。查询窗口取 90s（而非 90min）是**刻意**的：
窗口左上界跨过 `now-1min` 的实时区边界，既要「实时段能查回」，
又顺带覆盖 R-13 §4.3 的对照口径「同一查询在 `=true` 时返 0」。
物化表从未 refresh 过（`WITH NO DATA`），故 `=true` 时该窗口必为 0 ⇒ 断言可失败。

为什么灌 `[now-30min, now]` 却查 `[now-90min, now]`
--------------------------------------------------
30min 数据完整落在两视图的**实时段**内（实时段 = 物化水位到 `now()` 之间，由引擎对
`robot_telemetry` 透明回填）。查询窗口取 90min 是为了同时覆盖「物化段 + 实时段」的边界，
确保断言的是「经 cagg 能否查回窗内数据」；若 `materialized_only=true`，查询视图只到物化水位
（本用例从未 refresh 过，物化表为空）⇒ 该窗口返 0，断言即红。

无 Docker 时
------------
**skip 并报明**（`pytest.skip` / 退出码 3），绝不 red。判定顺序与 `test_e2e_30k.py` 一致：
`docker` 命令在不在 → `docker info`（daemon 是否可达）→ `docker compose` 插件是否可用。
每一层都给出「为什么 skip」与「需要什么才能验证」，不把环境问题伪装成代码失败。

跑法
----
    # 有 pytest
    python -m pytest tests/e2e/test_cagg_realtime_backfill.py -v -s
    # 无 pytest（本文件自带 main，退出码 0=通过 1=断言失败 3=环境不具备）
    python tests/e2e/test_cagg_realtime_backfill.py

与 `test_e2e_30k.py` 的关系
---------------------------
`test_e2e_30k.py` 的 e2e stage 里栈已经起着，故本文件的断言函数
`assert_cagg_realtime_backfill()` 也被它**在栈还活着时调用一次**（「并入现有 e2e」，
R-13 文档 §7）：这样 CI 的 `e2e-30k-landing` 无需任何配置改动即可产出本用例的真库证据，
而本文件同时保留独立可跑入口（便于定位）。

环境变量（都有默认值）：
    E2E_COMPOSE=<path>       覆盖 compose 文件路径
    E2E_KEEP=1               结束后保留栈（排查用）
    E2E_CAGG_ROBOT_ID        被测机器人 ID（默认 humanoid-001）
    E2E_CAGG_ROWS            灌入行数（默认 1800，覆盖 [now-30min, now]）
    E2E_CAGG_STATUS_ROWS     robot_status_10s 专用灌数行数（默认 300 = [now-30s, now]）
    E2E_CAGG_UP_TIMEOUT      起栈 + 等健康超时（秒，默认 300）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = Path(os.environ.get(
    "E2E_COMPOSE", ROOT / "tests" / "e2e" / "docker-compose.e2e.yml"))

ROBOT_ID = os.environ.get("E2E_CAGG_ROBOT_ID", "humanoid-001")
# 1800 行 = [now-30min, now] 按 10Hz（与 mock_fleet 的付费频率一致）。
ROWS = int(os.environ.get("E2E_CAGG_ROWS", "1800"))
UP_TIMEOUT_S = int(os.environ.get("E2E_CAGG_UP_TIMEOUT", "300"))
KEEP = os.environ.get("E2E_KEEP") == "1"

# 查询窗口：覆盖「物化段 + 实时段」边界，灌 30min 数据必然落在其中。
QUERY_SPAN = "90 minutes"

# R-16：`robot_status_10s`（看板最新态）的 `end_offset = 1 minute`，实时区只有
# `[now-1min, now]`，故它的查询窗口另给（90s 跨过该边界而非 90min）。
STATUS_QUERY_SPAN = "90 seconds"
# R-16：机器人侧灌数 = `[now-30s, now]` 按 10Hz = 300 行，必落在 1min 实时区内。
STATUS_ROWS = int(os.environ.get("E2E_CAGG_STATUS_ROWS", "300"))

PYTEST_SKIP_MARKER = "E2E-SKIP"

# 被测的三个 cagg。前两个是 `/history` 的两条数据源（R-13，**禁回退**）；
# 第三个是看板最新态（R-16 本单新增的断言对象）。
HISTORY_CAGGS = ("robot_history_10s", "robot_history_1h")
STATUS_CAGG = "robot_status_10s"
TARGET_CAGGS = (*HISTORY_CAGGS, STATUS_CAGG)

# 各 cagg 的灌数行数与查询窗口：
#   history 两视图：`[now-30min, now]` 1800 行（10Hz），查 `[now-90min, now]`；
#   status 视图：   `[now-30s, now]`  300 行（10Hz），查 `[now-90s, now]`（只试实时区）。
CAGG_PLAN = {
    "robot_history_10s": ("90 minutes", ROWS),
    "robot_history_1h": ("90 minutes", ROWS),
    STATUS_CAGG: (STATUS_QUERY_SPAN, STATUS_ROWS),
}


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


def compose(*args: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    return run(["docker", "compose", "-f", str(COMPOSE_FILE), *args],
               timeout=timeout, check=check)


def require_docker() -> None:
    """无 Docker 就 skip（带明确原因），绝不 red。"""
    if shutil.which("docker") is None:
        raise E2ESkip(
            "PATH 内找不到 docker 命令。本用例需要真实 TimescaleDB"
            "（timescale/timescaledb:2.13.1-pg16），且**禁止**用 mock 替身（ISSUE #60 禁止项）。"
            "需要：在装了 Docker 的机器/runner 上运行。"
        )
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise E2ESkip(
            "docker daemon 不可达（`docker info` 非 0）。"
            f"原始输出：{(probe.stderr or probe.stdout).strip()[:400]}"
        )
    if not COMPOSE_FILE.is_file():
        raise E2ESkip(f"compose 文件不存在：{COMPOSE_FILE}")
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise E2ESkip(
            "`docker compose`（插件版）不可用，`docker compose version` 非 0。原始输出："
            f"{(probe.stderr or probe.stdout).strip()[:400]}"
        )


# ------------------------------------------------------------------ SQL 助手

def _psql(sql: str, timeout: int = 60) -> str:
    """在 timescaledb 容器里执行 SQL，返回单值（去空白）。"""
    proc = compose("exec", "-T", "timescaledb", "psql", "-U", "postgres",
                   "-d", "robot_monitor", "-tAc", sql, timeout=timeout)
    return proc.stdout.strip()


def wait_db_ready(timeout: int = UP_TIMEOUT_S) -> None:
    """等 TimescaleDB 真正可用。

    只等容器 healthy 不够：`init.sql` 由 docker-entrypoint 在**首启**执行，
    若在它跑完前就查 cagg 会得到「视图不存在」的假失败。这里等
    `timescaledb` 扩展 + 三个目标 cagg 都就位（cq.continuous_aggregate 目录有行）。
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        probe = compose("exec", "-T", "timescaledb", "psql", "-U", "postgres",
                        "-d", "robot_monitor", "-tAc",
                        "SELECT count(*) FROM timescaledb_information.continuous_aggregates "
                        "WHERE view_name IN ("
                        + ", ".join(f"'{c}'" for c in TARGET_CAGGS) + ");",
                        check=False, timeout=30)
        if probe.returncode == 0:
            last = probe.stdout.strip()
            if last == str(len(TARGET_CAGGS)):
                return
        else:
            last = (probe.stderr or probe.stdout).strip()[-200:]
        time.sleep(2)
    raise AssertionError(
        f"TimescaleDB 在 {timeout}s 内未就绪（{len(TARGET_CAGGS)} 个 cagg 未建齐，"
        f"最后探测：{last!r}）。"
        "多半是 infra/init.sql 在容器首启时执行失败 —— 请查 "
        f"`docker compose -f {COMPOSE_FILE} logs timescaledb`。"
    )


# ------------------------------------------------------------------ 断言主体

def assert_cagg_realtime_backfill(psql_fn=None, robot_id: str = ROBOT_ID,
                                  rows: int = ROWS, span: str = QUERY_SPAN) -> dict:
    """对**已经在跑**的真 TimescaleDB 栈断言实时回填（供独立入口与 test_e2e_30k 共用）。

    `psql_fn`：单值 SQL 执行器（默认走本文件的 `_psql`；`test_e2e_30k.py` 可传它自己的
    `psql`，从而在该 e2e 已起的同一栈上复用，不必重起一次）。
    `rows` / `span`：`/history` 两视图的灌数行数与查询窗口（保持 R-13 既有签名与默认值，
    调用方无需改动）；R-16 的 `robot_status_10s` 用本文件 `CAGG_PLAN` 里的
    `STATUS_ROWS` / `STATUS_QUERY_SPAN`（1min 实时区口径不同，见文件头说明）。

    返回取证字典（灌入行数、三视图 count、materialized_only 实测值、viewdef 是否含实时段）。
    """
    q = psql_fn or _psql

    # 各视图本次要灌的行数与查询窗口。
    plan = {c: (span, rows) for c in TARGET_CAGGS}
    plan[STATUS_CAGG] = (STATUS_QUERY_SPAN, STATUS_ROWS)

    # 0) 前置事实：确认连的是**真 TimescaleDB**（不是 PG 替身，ISSUE #60 禁 mock）。
    version = q("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';")
    if not version:
        raise AssertionError(
            "连到的库没有 timescaledb 扩展 —— 不是真 TimescaleDB。"
            "本用例禁止用 mock 替身（ISSUE #60）。"
        )

    # 1) 属性证据：三视图的 materialized_only 必须为 false（=实时聚合）。
    #    这是「配置层」的直证：一旦 init.sql §5.1c / §5.1d 被删/被改回 true，这里先红。
    mo = {}
    for cagg in TARGET_CAGGS:
        val = q(
            "SELECT materialized_only FROM _timescaledb_catalog.continuous_agg "
            f"WHERE user_view_name = '{cagg}';"
        )
        mo[cagg] = val
        if val != "f":
            if cagg == STATUS_CAGG:
                hint = (
                    "若为 't'：init.sql §5.1d 的 `ALTER MATERIALIZED VIEW IF EXISTS "
                    "robot_status_10s SET (timescaledb.materialized_only = false)` 未生效"
                    "或被回退 —— 这正是本 R-16 要修的 bug，看板最新态会滞后约 1min。"
                )
            else:
                hint = (
                    "若为 't'：init.sql §5.1c 的 `SET (timescaledb.materialized_only = false)` "
                    "未生效或被回退（R-13 回归）—— `/history` 右端会出现空洞。"
                )
            raise AssertionError(
                f"{cagg} 的 materialized_only = {val!r}，期望 'f'（false=实时聚合）。{hint}"
            )

    # 2) viewdef 旁证：实时聚合的查询视图必含 `UNION ALL ... FROM robot_telemetry` 实时段。
    #    纯物化视图只有 `FROM _materialized_hypertable_N`。
    viewdef_has_realtime = {}
    for cagg in TARGET_CAGGS:
        vd = q(f"SELECT pg_get_viewdef('{cagg}');")
        has_union = "UNION ALL" in vd.upper() and "robot_telemetry" in vd
        viewdef_has_realtime[cagg] = has_union
        if not has_union:
            raise AssertionError(
                f"{cagg} 的 pg_get_viewdef 不含 `UNION ALL ... FROM robot_telemetry` 实时段 "
                f"（materialized_only={mo[cagg]!r}）—— 说明它不是实时聚合视图。"
            )

    # 3) 灌数：向 robot_telemetry 插数据（单台）。
    #    用 generate_series 在库内生成，避免几十行 SQL 文本与时间格式歧义；
    #    payload 各列取满足 NOT NULL 的最小合法值（JSONB 关节列给 '[]'）。
    #    ⚠️ 每张视图**单独**灌：`robot_status_10s` 的实时区只有 `[now-1min, now]`，
    #    若与 30min 的 history 灌数合并，时间上会多出无关行，使「查回的数据究竟来自
    #    哪一段」不可分辨 —— 本 R-16 要证的恰是「最近 30 秒的新数据能被查回」。
    inserted = {}
    for cagg in TARGET_CAGGS:
        _, n = plan[cagg]
        inserted[cagg] = int(q(
            "WITH ins AS ("
            "  INSERT INTO robot_telemetry ("
            "    time, robot_id, status_code, error_code, battery_pct, max_joint_temp,"
            "    joint_temperatures, joint_positions, joint_torques"
            "  ) SELECT"
            f"    now() - make_interval(secs => (g * 1.0)),"
            f"    '{robot_id}', 1, 0, 88.0, 42.0, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb"
            f"  FROM generate_series(0, {n - 1}) AS g"
            "  RETURNING 1"
            ") SELECT count(*) FROM ins;"
        ))
        if inserted[cagg] != n:
            raise AssertionError(
                f"为 {cagg} 灌数失败：期望 {n} 行，实际写入 {inserted[cagg]} 行。"
            )

    # 4) 核心断言：三 cagg 查各自窗口均必须 > 0（实时回填生效）。
    counts = {}
    for cagg in TARGET_CAGGS:
        s_span, _ = plan[cagg]
        counts[cagg] = int(q(
            f"SELECT count(*) FROM {cagg} "
            f"WHERE bucket >= now() - INTERVAL '{s_span}' AND robot_id = '{robot_id}';"
        ))

    # 5) 底层对照：确认数据真的进到 robot_telemetry（否则 cagg=0 是灌数问题，不是回填问题）。
    raw = {}
    for cagg in TARGET_CAGGS:
        s_span, _ = plan[cagg]
        raw[cagg] = int(q(
            "SELECT count(*) FROM robot_telemetry "
            f"WHERE time >= now() - INTERVAL '{s_span}' AND robot_id = '{robot_id}';"
        ))

    evidence = {
        "timescaledb_version": version,
        "materialized_only": mo,
        "viewdef_has_realtime": viewdef_has_realtime,
        "inserted": inserted,
        "raw_rows_in_span": raw,
        "cagg_counts": counts,
        "plan": {c: {"span": plan[c][0], "rows": plan[c][1]} for c in TARGET_CAGGS},
        "robot_id": robot_id,
        "query_span": span,
    }

    for cagg, c in counts.items():
        if c <= 0:
            s_span, n = plan[cagg]
            if cagg == STATUS_CAGG:
                hint = (
                    f"materialized_only={mo[cagg]!r}（期望 'f'）。若为 't'：init.sql §5.1d 的 "
                    "`SET (timescaledb.materialized_only = false)` 被删/被回退，"
                    "看板最新态缺最近 1min（本 R-16 正是要修它）。"
                )
            else:
                hint = (
                    f"materialized_only={mo[cagg]!r}（期望 'f'）。若为 't'：init.sql §5.1c 的 "
                    "`SET (timescaledb.materialized_only = false)` 被删/被回退，"
                    "`/history` 右端出现空洞（R-13 回归）。"
                )
            raise AssertionError(
                f"{cagg} 在 `bucket >= now()-'{s_span}' AND robot_id={robot_id!r}` 上返回 0，"
                f"但底层 robot_telemetry 同窗口有 {raw[cagg]} 行（本轮为它灌了 {n} 行）"
                f" —— 实时聚合回填**未生效**。{hint}"
            )
    return evidence


def render_report(ev: dict) -> str:
    lines = [
        "== cagg 实时回填回归（真 TimescaleDB）：R-13 /history ×2 + R-16 看板最新态 ==",
        f"TimescaleDB 版本：{ev['timescaledb_version']}",
        f"materialized_only：{ev['materialized_only']}（期望三视图均 'f'）",
        f"viewdef 含实时段：{ev['viewdef_has_realtime']}",
        f"灌入行数：{ev['inserted']}（robot_id={ev['robot_id']}）",
        f"底层 robot_telemetry 窗口内行数：{ev['raw_rows_in_span']}",
        f"cagg 计数（按各自窗口）：{ev['cagg_counts']}；计划={ev['plan']}",
        "结论：**通过** —— 三 cagg 均为实时聚合且查回窗内数据（>0）。",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ 起/停栈（独立入口用）

def up_timescaledb() -> None:
    """只起 timescaledb（本用例不需要 emqx/backend/redis，也不跑 mock_fleet）。"""
    compose("up", "-d", "timescaledb", timeout=UP_TIMEOUT_S)
    wait_db_ready()


def teardown() -> None:
    if KEEP:
        print("[cagg] E2E_KEEP=1，保留栈（手动清理："
              f"docker compose -f {COMPOSE_FILE} down -v）", flush=True)
        return
    # down -v：连卷一起删，保证下轮是**全新库 + init.sql 重跑**。
    compose("down", "-v", "--remove-orphans", check=False, timeout=120)


# ------------------------------------------------------------------ pytest / 裸跑入口

def _pytest_skip(reason: str):
    if under_pytest():
        import pytest  # noqa: PLC0415

        pytest.skip(reason)
    print(f"{PYTEST_SKIP_MARKER}: {reason}", flush=True)
    sys.exit(3)


def under_pytest() -> bool:
    return "pytest" in sys.modules and __name__ != "__main__"


def test_cagg_realtime_backfill() -> None:
    try:
        require_docker()
    except E2ESkip as exc:
        _pytest_skip(str(exc))

    try:
        up_timescaledb()
        ev = assert_cagg_realtime_backfill()
        print("\n" + render_report(ev) + "\n", flush=True)
    finally:
        teardown()


def main() -> int:
    """无 pytest 时的入口：0=通过 1=断言失败 3=环境不具备（skip）。"""
    try:
        require_docker()
    except E2ESkip as exc:
        print(f"{PYTEST_SKIP_MARKER}: {exc}", flush=True)
        return 3

    try:
        up_timescaledb()
    except AssertionError as exc:
        print(f"[cagg] 起栈/就绪失败：{exc}", flush=True)
        _dump_logs()
        teardown()
        return 1

    try:
        ev = assert_cagg_realtime_backfill()
    except AssertionError as exc:
        print(f"[cagg] 断言未通过：{exc}", flush=True)
        _dump_logs()
        teardown()
        return 1

    print("\n" + render_report(ev), flush=True)
    teardown()
    return 0


def _dump_logs() -> None:
    """失败取证：抓 timescaledb 容器尾部日志（init.sql 报错在这里可见）。"""
    try:
        proc = compose("logs", "--tail=80", "timescaledb", timeout=30, check=False)
        print("\n===== compose logs timescaledb (tail 80) =====", flush=True)
        print((proc.stdout or "")[-8000:] or "(no output)", flush=True)
    except Exception as exc:  # 取证本身绝不允许改变退出码
        print(f"(取证失败: {exc})", flush=True)


if __name__ == "__main__":
    sys.exit(main())
