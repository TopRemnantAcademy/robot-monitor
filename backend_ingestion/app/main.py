"""FastAPI 入口（spec/01 §4.5 + spec/00 §5.2/§5.3）。

对外契约（缺一即 blocker）：
- WS  `/ws/robots`   —— 出站必须是**带 type 的信封**：snapshot / telemetry / alert
                        且必须订阅 Redis `robot:alerts` 转发告警（否则前端告警流永远空白）
- REST `GET /robots`                     —— 100 台最新状态（WS 之外兜底）
- REST `GET /robots/{id}/history`        —— Drawer 的 ECharts 折线与关节热力图数据源
- REST `GET /healthz`                    —— 健康检查
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.config import settings
from app.db.redis import close_redis, get_redis
from app.db.session import close_pool
from app.services import mqtt_subscriber, state_cache
from app.services.batch_writer import BatchWriter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("main")

writer = BatchWriter()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    writer_task = asyncio.create_task(writer.run())
    mqtt_task = asyncio.create_task(mqtt_subscriber.run(writer))
    app.state.writer_task = writer_task
    app.state.mqtt_task = mqtt_task
    log.info("backend started: mqtt=%s:%d", settings.mqtt_broker, settings.mqtt_port)
    yield
    # shutdown
    for t in (mqtt_task, writer_task):
        t.cancel()
    await writer.graceful_shutdown()
    await asyncio.gather(mqtt_task, writer_task, return_exceptions=True)
    await close_pool()
    await close_redis()
    log.info("backend stopped")


app = FastAPI(title="RobotMonitor Ingestion", lifespan=lifespan)


# ------------------------------------------------------------------ REST

@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "written": writer.written,
        "flushes": writer.flushes,
        "queue_depth": writer.queue.qsize(),
        "mqtt_received": mqtt_subscriber.received,
        "mqtt_dropped": mqtt_subscriber.dropped,
    }


@app.get("/robots")
async def list_robots():
    return {"robots": await state_cache.snapshot(settings.robot_ids)}


@app.get("/robots/{robot_id}/history")
async def history(robot_id: str, minutes: int = 5, resolution: str = "10s"):
    """最近 N 分钟历史（供 Drawer 折线 + 关节热力图）。

    说明：spec/00 §4.3 的连续聚合 `robot_status_10s` **不含 JSONB 关节列**，
    而 Drawer 的热力图需要 joint_temperatures，故此处直接对原始表做 time_bucket，
    一次性取回标量 + 关节字典，避免二次查询。
    """
    try:
        res_s = _parse_resolution(resolution)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid resolution: {resolution}")

    span_s = max(1, minutes) * 60
    sql = """
        SELECT time_bucket(($1 || ' seconds')::interval, time) AS bucket,
               robot_id,
               last(status_code, time)        AS status_code,
               last(battery_pct, time)        AS battery_pct,
               max(max_joint_temp)            AS max_joint_temp,
               last(joint_temperatures, time) AS joint_temperatures,
               last(joint_torques, time)      AS joint_torques,
               -- joint_positions 必须返回：前端 Drawer 的第三条折线「角度(rad)」
               -- 数据源就是 point.joint_positions[joint]（RobotDetailDrawer.tsx:93）。
               -- 缺失时该曲线恒为 null，图上永远空白。
               last(joint_positions, time)    AS joint_positions
        FROM robot_telemetry
        WHERE robot_id = $2
          AND time >= now() - (($3 || ' seconds')::interval)
        GROUP BY bucket, robot_id
        ORDER BY bucket
    """
    from app.db.session import get_pool

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, str(res_s), robot_id, str(span_s))
    except Exception as exc:
        log.exception("history query failed")
        return JSONResponse(status_code=503, content={"detail": f"query failed: {exc}"})

    return {
        "robot_id": robot_id,
        "resolution": f"{res_s}s",
        "points": [
            {
                "time": r["bucket"].isoformat(),
                "status_code": r["status_code"],
                "battery_pct": r["battery_pct"],
                "max_joint_temp": r["max_joint_temp"],
                "joint_temperatures": _as_dict(r["joint_temperatures"]),
                "joint_torques": _as_dict(r["joint_torques"]),
                "joint_positions": _as_dict(r["joint_positions"]),
            }
            for r in rows
        ],
    }


def _parse_resolution(resolution: str) -> int:
    """'10s' / '30s' / '1m' → 秒。"""
    s = resolution.strip().lower()
    if s.endswith("ms"):
        raise ValueError("resolution below 1s not supported")
    if s.endswith("m"):
        return max(1, int(float(s[:-1])) * 60)
    if s.endswith("s"):
        return max(1, int(float(s[:-1])))
    raise ValueError(resolution)


def _as_dict(v) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return {}
    return {}


# ------------------------------------------------------------------ WebSocket

@app.websocket("/ws/robots")
async def ws_robots(ws: WebSocket):
    await ws.accept()
    r = await get_redis()

    # ① 首帧：100 台全量快照
    try:
        snap = await state_cache.snapshot(settings.robot_ids)
        await ws.send_json({"type": "snapshot", "data": {"robots": snap}})
    except Exception:
        log.exception("snapshot failed")

    # ② 增量：遥测 + 告警（alert 通道是前端告警流的唯一数据源）
    pubsub = r.pubsub()
    await pubsub.subscribe(settings.chan_stream_all, settings.chan_alerts)
    try:
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue
            try:
                data = json.loads(msg["data"])
            except json.JSONDecodeError:
                continue
            channel = msg["channel"]
            if channel == settings.chan_alerts:
                await ws.send_json({"type": "alert", "data": data})
            elif channel == settings.chan_stream_all:
                await ws.send_json({"type": "telemetry", "data": data})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws error")
    finally:
        try:
            await pubsub.unsubscribe(settings.chan_stream_all, settings.chan_alerts)
            await pubsub.aclose()
        except Exception:
            pass
