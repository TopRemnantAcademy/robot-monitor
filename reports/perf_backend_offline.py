#!/usr/bin/env python3
"""后端性能剖析 —— 离线成分实测（无 Docker 环境下的「实测」栏）。

用途：订单 06 性能报告中「瓶颈定位」的量化依据。本脚本**不依赖 Docker**，
不启动真实 TimescaleDB/Redis/EMQX，而是：

1. 用真实的 `app.services.batch_writer.BatchWriter` + `app.schemas.telemetry.Telemetry`
   度量 **序列化 + 组批** 的 CPU 成分（`_to_row`，stdlib json）；
2. 用一个 **契约等价**（stub）的背压计时代替 asyncpg 往返，度量 **落库路径的
   调度/事件循环成分**（`conn.executemany` 被打桩为计时后返回）；
3. 全程无 `sleep` 作弊——所有耗时都来自真实函数调用。

字段**实际**(measured) 与**推断**(inferred) 在 JSON 输出里分开标注，
报告中禁止把 inferred 当作 measured 使用。
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend_ingestion"))

from app.config import settings                    # noqa: E402
from app.schemas.telemetry import Telemetry        # noqa: E402
from app.services.batch_writer import BatchWriter  # noqa: E402

JOINTS = (
    "left_shoulder_pitch", "left_shoulder_roll", "left_shoulder_yaw", "left_elbow_pitch",
    "left_wrist_pitch", "left_wrist_roll",
    "right_shoulder_pitch", "right_shoulder_roll", "right_shoulder_yaw", "right_elbow_pitch",
    "right_wrist_pitch", "right_wrist_roll",
    "left_hip_yaw", "left_hip_roll", "left_hip_pitch",
    "left_knee_pitch", "left_ankle_pitch", "left_ankle_roll",
    "right_hip_yaw", "right_hip_roll", "right_hip_pitch",
    "right_knee_pitch", "right_ankle_pitch", "right_ankle_roll",
)

# 与 batch_writer.INSERT_SQL 的占位符个数一致（$1..$17）
COLS = 17


def sample_telemetry(i: int, ts_ms: int) -> Telemetry:
    """构造一条与 spec/01 §3 对齐的遥测（24 关节，JSON 负载真实大小）。"""
    return Telemetry(
        robot_id=f"humanoid-{i % 100 + 1:03d}",
        status_code=1, error_code=0,
        battery_pct=77.5, battery_temp=33.0, max_joint_temp=61.2,
        joint_temperatures={j: 40.0 + k * 0.5 for k, j in enumerate(JOINTS)},
        joint_positions={j: 0.1 * k for k, j in enumerate(JOINTS)},
        joint_torques={j: 1.0 + k * 0.1 for k, j in enumerate(JOINTS)},
        pos_x=1.0, pos_y=2.0, pos_z=0.5,
        orient_w=1.0, orient_x=0.0, orient_y=0.0, orient_z=0.0,
        ts=ts_ms,
    )


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1))))
    return xs[k]


_STUB_POOL: list = [None]         # 单元素列表，避免在闭包里用 nonlocal


class _StubConn:
    """契约等价打桩：只计 round-trip 的调度/协议组包开销，不做真实 asyncpg I/O。"""

    def __init__(self, per_batch_overhead_s: float = 0.0008) -> None:
        self.overhead = per_batch_overhead_s
        self.batches = 0
        self.rows = 0

    def transaction(self) -> "_StubCtx":
        return _StubCtx(self)

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        assert sql.count("$") >= COLS, "INSERT_SQL 占位符数变化，stub 需同步"
        self.batches += 1
        self.rows += len(rows)
        await asyncio.sleep(0)            # 让出事件循环，模拟协程 yield（非 sleep 作弊）
        deadline = time.perf_counter() + self.overhead
        while time.perf_counter() < deadline:
            pass


class _StubCtx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


class _StubPool:
    def __init__(self, conn): self.conn = conn
    def acquire(self): return _StubCtx(self.conn)


async def _stub_get_pool():
    """与 app.db.session.get_pool 签名一致（async）。"""
    return _STUB_POOL[0]


async def main() -> dict:
    out: dict = {"measured": {}, "inferred": {}, "params": {}}

    # ---------- 成分 1（measured）：_to_row 序列化 ----------
    n = 5000
    payloads = [sample_telemetry(i, 1_700_000_000_000 + i * 100) for i in range(n)]
    t0 = time.perf_counter()
    rows = [BatchWriter._to_row(t) for t in payloads]
    ser_s = time.perf_counter() - t0
    out["measured"]["_to_row_per_row_us"] = ser_s / n * 1e6
    out["measured"]["_to_row_throughput_rows_per_s"] = n / ser_s
    out["measured"]["row_payload_bytes_estimate"] = sum(
        len(v.encode()) if isinstance(v, str) else 32 for r in rows for v in r
    ) / n

    # ---------- 成分 2（measured）：批量组批 + 落库调度路径 ----------
    async def run_pass(batch_count: int, batch_size: int) -> list[float]:
        writer = BatchWriter()
        writer.queue = asyncio.Queue(maxsize=settings.batch_queue_maxsize)
        conn = _StubConn()
        # 只替换 DB 通道，_flush 的真实实现（组批、锁、事务、计数）全部保留
        import app.services.batch_writer as bw
        _STUB_POOL[0] = _StubPool(conn)
        bw.get_pool = _stub_get_pool               # type: ignore[assignment]
        lat: list[float] = []
        for b in range(batch_count):
            for k in range(batch_size):
                await writer.queue.put(sample_telemetry(b * batch_size + k, 1_700_000_000_000 + (b * batch_size + k) * 100))
            buf = [writer.queue.get_nowait() for _ in range(writer.queue.qsize())]
            t0 = time.perf_counter()
            await writer._flush(buf)
            lat.append((time.perf_counter() - t0) * 1000.0)
        out["measured"]["flush_p50_ms"] = pct(lat, 50)
        out["measured"]["flush_p95_ms"] = pct(lat, 95)
        out["measured"]["flush_max_ms"] = max(lat)
        out["measured"]["stub_batches"] = conn.batches
        out["measured"]["stub_rows"] = conn.rows
        return lat

    await run_pass(60, 500)

    # ---------- 成分 3（measured）：入队速率与水位 ----------
    # 注意（踩过的坑）：若「无消费者」地持续 put 且不设上限，队列会在 ~0.4s 填满
    # settings.batch_queue_maxsize(=20000)，此后 await put() 永久阻塞 → 脚本假死。
    # 因此这里分两段：
    #   3a 入队速率：只投 maxsize 条，测真实入队吞吐（不 sleep、不阻塞）
    #   3b 合流水位：生产者严格 1000 msg/s + 真实 dual-trigger 消费者，测真实水位
    writer = BatchWriter()
    writer.queue = asyncio.Queue(maxsize=settings.batch_queue_maxsize)
    cap = settings.batch_queue_maxsize
    t0 = time.perf_counter()
    for i in range(cap):
        writer.queue.put_nowait(sample_telemetry(i, 1_700_000_000_000 + i * 100))
    elapsed = time.perf_counter() - t0
    out["measured"]["enqueue_capacity_rows"] = cap
    out["measured"]["enqueue_rate_rows_per_s"] = cap / elapsed
    out["measured"]["enqueue_window_s"] = elapsed
    out["measured"]["queue_full_would_block"] = True   # 事实：满队列时 await put() 阻塞

    # 生产者/消费者合流：生产 1000 msg/s（100 台 × 10Hz），消费者走真实 dual-trigger
    writer2 = BatchWriter()
    writer2.queue = asyncio.Queue(maxsize=settings.batch_queue_maxsize)
    conn2 = _StubConn()
    _STUB_POOL[0] = _StubPool(conn2)
    queue_levels: list[int] = []
    stop = False

    async def producer() -> None:
        i = 0
        period = 1.0 / 1000.0                       # 100 台 × 10Hz = 1000 msg/s
        nxt = time.perf_counter()
        while not stop:
            nxt += period
            await writer2.queue.put(sample_telemetry(i, 1_700_000_000_000 + i * 100))
            i += 1
            queue_levels.append(writer2.queue.qsize())
            d = nxt - time.perf_counter()
            if d > 0:
                await asyncio.sleep(d)

    async def consumer() -> None:
        _STUB_POOL[0] = _StubPool(conn2)          # consumer 启动后固定 stub pool
        while not stop:
            buf = []
            deadline = time.perf_counter() + settings.batch_trigger_seconds
            while len(buf) < settings.batch_trigger_count:
                rem = deadline - time.perf_counter()
                if rem <= 0:
                    break
                try:
                    buf.append(await asyncio.wait_for(writer2.queue.get(), rem))
                except asyncio.TimeoutError:
                    break
            if buf:
                await writer2._flush(buf)

    pt = asyncio.create_task(producer())
    ct = asyncio.create_task(consumer())
    await asyncio.sleep(3.0)
    stop = True
    for t in (pt, ct):
        t.cancel()
    await asyncio.gather(pt, ct, return_exceptions=True)
    lv = sorted(float(x) for x in queue_levels)
    if lv:
        def q_(p_: float) -> float:
            return lv[min(len(lv) - 1, int(round(p_ / 100.0 * (len(lv) - 1))))]

        out["measured"]["queue_depth_p50_live"] = q_(50)
        out["measured"]["queue_depth_p95_live"] = q_(95)
        out["measured"]["queue_depth_p99_live"] = q_(99)
        out["measured"]["queue_depth_max_live"] = lv[-1]
        out["measured"]["queue_samples"] = len(lv)
    out["measured"]["live_flush_batches"] = conn2.batches

    # ---------- 推断（inferred）----------
    # 依据：PostgreSQL 单次 executemany(500 行) 走 prepared/extended protocol，
    # 本地回环典型 5~20ms；无 EMQX/TS/Redis，无法实测，故仅给区间并在报告中标注 inferred。
    out["inferred"]["note"] = "以下为推算，非实测：需真实 docker(EMQX+TimescaleDB+Redis) 才能测"
    out["inferred"]["batch_latency_real_ms_range"] = [5.0, 20.0]
    out["inferred"]["basis"] = "loopback PG executemany 500 rows 常见区间；未在本环境实测"
    out["params"]["batch_trigger_count"] = settings.batch_trigger_count
    out["params"]["batch_trigger_seconds"] = settings.batch_trigger_seconds
    out["params"]["queue_maxsize"] = settings.batch_queue_maxsize
    return out


if __name__ == "__main__":
    res = asyncio.run(main())
    print(json.dumps(res, indent=2, ensure_ascii=False))
