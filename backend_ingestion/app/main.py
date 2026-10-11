"""FastAPI 入口（spec/01 §4.5 + spec/00 §5.2/§5.3）。

对外契约（缺一即 blocker）：
- WS  `/ws/robots`   —— 出站必须是**带 type 的信封**：snapshot / telemetry / alert
                        且必须订阅 Redis `robot:alerts` 转发告警（否则前端告警流永远空白）
- REST `GET /robots`                     —— 100 台最新状态（WS 之外兜底）
- REST `GET /robots/{id}/history`        —— Drawer 的 ECharts 折线与关节热力图数据源
- REST `GET /alerts`                     —— 告警分页（订单 17，viewer 起可读）
- REST `POST /alerts/{id}/ack|unack`     —— 告警确认/撤回（订单 17，需 admin）
- REST `GET /healthz`                    —— 健康检查
- REST `GET /metrics`                    —— Prometheus 抓取（**必须免鉴权**）

鉴权边界（订单 15）：
- 免鉴权白名单只有两个：`/healthz`（存活探针）与 `/metrics`（Prometheus **pull**）。
  给探针加鉴权会打挂容器编排；给 `/metrics` 加鉴权等于监控静默失效。
- 其余 REST 走 `Authorization: Bearer <JWT>`（**不接受** query 传 token，
  否则 token 会进 nginx/网关的访问日志）；
- `/ws/robots` 走 `?token=`（浏览器 WebSocket API 无法自定义请求头，只能走 query）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

from app import metrics, telemetry_tracing
from app.alerts.router import router as alerts_router
from app.auth.deps import authenticate_ws_token, current_active_user
from app.auth.router import router as auth_router
from app.auth.users import RobotUser, close_engine
from app.config import settings
from app.db.redis import close_redis, get_redis
from app.db.session import close_pool, ping
from app.logging_json import configure_json_logging
from app.schemas.api import (
    HealthResponse,
    RobotHistoryResponse,
    RobotListResponse,
)
from app.services import mqtt_subscriber, state_cache
from app.services.batch_writer import BatchWriter
from app import ws_probe

# 订单 16 T4：单行 JSON 日志，供订单 07 的 openobserve/loki 直接采集。
# 必须在**其它 logger 首次输出之前**配置 —— uvicorn 的启动日志会经由根 logger，
# 晚配的话前几行仍是文本，采集侧就会看到混合格式。
configure_json_logging(logging.INFO)
log = logging.getLogger("main")

writer = BatchWriter()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    writer_task = asyncio.create_task(writer.run())
    mqtt_task = asyncio.create_task(mqtt_subscriber.run(writer))
    # WS 合成探针：客户端视角验活（缺口 G-3）。它只读指标，不碰数据流；
    # 关闭时 `run()` 立刻 return，任务自灭，无需在 shutdown 里特判。
    probe_task = asyncio.create_task(ws_probe.run())
    app.state.writer_task = writer_task
    app.state.mqtt_task = mqtt_task
    app.state.probe_task = probe_task
    # 订单 16 T3：endpoint 为空则 no-op，返回 False 也不影响启动（可降级）。
    # 放在 yield 之前：instrument 必须早于第一个请求到达。
    tracing_on = telemetry_tracing.configure_tracing(
        settings.otel_exporter_otlp_endpoint, settings.otel_service_name
    )
    if tracing_on:
        telemetry_tracing.instrument_fastapi(app)
    log.info(
        "backend started: mqtt=%s:%d tracing=%s",
        settings.mqtt_broker,
        settings.mqtt_port,
        "on" if tracing_on else "off",
    )
    yield
    # shutdown —— 顺序不可调换，官方模式见
    # external/aiomqtt/docs/alongside-fastapi-and-co.md @ v2.5.1
    #   ① 先停 MQTT，掐断入口，不再产生新遥测
    mqtt_task.cancel()
    await asyncio.gather(mqtt_task, return_exceptions=True)
    #   ② 冲刷 writer 存量：graceful_shutdown() 投哨兵唤醒 run()，
    #      再 await writer_task 等它把手上这批刷完才返回。
    #      若在此前就 cancel writer_task，run() 会在 _flush 中途被中断，
    #      最后一批遥测（订单 06 验收关注的 30000 行附近）会丢。
    await writer.graceful_shutdown()
    await asyncio.gather(writer_task, return_exceptions=True)
    #   ③ 探针只是旁观者，没有存量要冲刷，直接取消（它 slept 在 interval 上）。
    probe_task.cancel()
    await asyncio.gather(probe_task, return_exceptions=True)
    await close_pool()
    await close_redis()
    # auth 的 ORM engine 是独立于 asyncpg 业务池的（fastapi-users 要 SQLAlchemy 会话），
    # 同样要在收尾时释放，否则容器退出时连接会挂到服务端超时为止。
    await close_engine()
    # 最后再收尾 trace：BatchSpanProcessor 队列里可能还压着关闭阶段的 span。
    telemetry_tracing.shutdown_tracing()
    log.info("backend stopped")


app = FastAPI(title="RobotMonitor Ingestion", lifespan=lifespan)

# 认证路由（/auth/register、/auth/jwt/login、/auth/jwt/logout、/users/me）。
# 它们**不加**任何鉴权依赖 —— 登录端点自己再要求 token 就成了死循环。
app.include_router(auth_router)

# 告警闭环（订单 17）—— GET /alerts 分页 + POST /alerts/{id}/ack|unack。
# 挂在同一 app 下，路径为绝对路径（/alerts…），不带前缀。
app.include_router(alerts_router)


# ------------------------------------------------------------------ REST

@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """健康检查。

    `status` 反映**依赖可达性**，不只是「进程活着」：
    - `ok`       —— DB 与 Redis 都可达
    - `degraded` —— 进程活着但依赖不可达（此时写入会堆积到队列，需立刻告警）
    - 仍返回 HTTP 200：这是**存活探针**语义，改为 503 会让容器被重启掉，
      而依赖恢复后本服务能自愈，重启反而丢在飞数据。依赖不健康由 `status` 字段表达。
    """
    db_ok = await _db_healthy()
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "up" if db_ok else "down",
        "written": writer.written,
        "flushes": writer.flushes,
        "retries": writer.retries,
        "rejected": writer.rejected,
        "queue_depth": writer.queue.qsize(),
        "mqtt_received": mqtt_subscriber.received,
        "mqtt_dropped": mqtt_subscriber.dropped,
        # 丢行三元组（ISSUE #19 G2）：submitted 是交给落库的行数，
        # written 是真正入库的，skipped 是 ON CONFLICT DO NOTHING 静默丢弃的。
        # 三者与 rejected（降级链判定不可写的坏行）合起来才是"收到多少 / 入库多少"的完整答案。
        "rows_submitted": writer.rows_submitted,
        "rows_written": writer.written,
        "rows_skipped": writer.rows_skipped,
    }


# 当前活跃 WS 连接数（订单 16 T1b 的 `robot_monitor_ws_connections`）。
# 进程内整数而非 Gauge 直写：Gauge.set 在 /metrics 里统一刷新（拉模型），
# 这里维护的"事实"必须在**连接真正建立/回收**时更新，两个时点不能混。
# 用 try/finally 保证异常路径也会回收，否则连接数只增不减（假告警）。
_ws_active = 0


_db_probe: tuple[float, bool] | None = None


async def _db_healthy() -> bool:
    """带缓存的 DB 探活。

    缓存的作用：探针会被容器编排以秒级频率调用，每次都打库既浪费连接也放大故障。
    缓存窗口（settings.db_health_cache_s）内复用上次结果，故障时把探测频率降到窗口频率。
    """
    global _db_probe
    now = asyncio.get_running_loop().time()
    if _db_probe is not None and now - _db_probe[0] < settings.db_health_cache_s:
        return _db_probe[1]
    ok = await ping()
    _db_probe = (now, ok)
    return ok


@app.get(
    "/metrics",
    # 显式声明 text/plain：Prometheus exposition format 不是 JSON。
    # 不声明时 FastAPI 会默认插入 `application/json` + 空 schema，
    # 快照里的契约就写成 JSON —— 与真实响应体不符，
    # 正是本订单要消灭的"文档与契约漂移"。
    # 用 response_class=PlainTextResponse 才能**替换掉**那个默认 content-type
    # （只写 responses={...} 只会追加一条，JSON 那条仍在）。
    response_class=PlainTextResponse,
)
async def prometheus_metrics():
    """Prometheus 抓取端点（`infra/observability/prometheus.yml` 的 `robot_backend` job）。

    与 `/healthz` 的区别是**模型**而不是内容：`/healthz` 回瞬时快照，
    抓取端拿到的永远是"上一次写入那一刻"的值；这里的 Counter 是单调累加的，
    Prometheus 才能算出 `rate()` / `increase()`，让 1000 msg/s 有历史曲线。

    函数名**不能叫 `metrics`** —— 那会遮蔽模块名 `app.metrics`（`from app import metrics`）。
    """
    # 瞬时量（Gauge）必须在暴露前刷新：拉模型下没人替我们定期 set，
    # 不刷新的话 queue_depth 会停在最后一次写入的时刻，抓到的永远是过期值。
    # 订单 16 验收要求 `robot_monitor_batch_queue_size` 压测时有真实波动（非常量 0），
    # 正是靠这一次 set —— 少了它，Gauge 初值是 0 且永不变。
    metrics.queue_depth.set(writer.queue.qsize())
    metrics.refresh_gauges(writer.queue.qsize(), _ws_active)
    body, ctype = metrics.render()
    # ctype 由 prometheus_client 给出（`text/plain; version=0.0.4; charset=utf-8`），
    # 必须原样透传 —— 版本号是 Prometheus 解析器协商格式的依据，不能丢。
    return PlainTextResponse(content=body, media_type=ctype)


@app.get("/robots", response_model=RobotListResponse)
async def list_robots(_user: RobotUser = Depends(current_active_user)):
    """100 台最新状态。只读端点：viewer 起可访问，返回体与加鉴权前逐字段一致。"""
    return {"robots": await state_cache.snapshot(settings.robot_ids)}


@app.get("/robots/{robot_id}/history", response_model=RobotHistoryResponse)
async def history(
    robot_id: str,
    minutes: int = 5,
    resolution: str = "10s",
    _user: RobotUser = Depends(current_active_user),
):
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
    # 订单 16 T1b：/history 耗时直方图。
    # 计时**只覆盖 DB 查询**（含 acquire），不含参数校验与结果序列化 ——
    # 前者是数据库问题、后者是 CPU 问题，混在一个桶里会让"该不该建连续聚合表"
    # 这个决策失去依据（序列化耗时不随数据量线性增长，查询耗时才会）。
    # try/finally 而非只在成功时 observe：**查询失败的那一刻往往正是最慢的一次**
    # （超时/锁等待），漏掉它会让 p99 被系统性低估。
    _t0 = time.perf_counter()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, str(res_s), robot_id, str(span_s))
    except Exception as exc:
        log.exception("history query failed")
        return JSONResponse(status_code=503, content={"detail": f"query failed: {exc}"})
    finally:
        metrics.history_latency_seconds.observe(time.perf_counter() - _t0)

    # 注意：这里**刻意不校验 robot_metadata**。init.sql 预置了 100 台机器人，
    # 但「从未上报过数据的机器人」查历史必然是空数组——这是正常语义而非错误。
    # 若改成 404，Drawer 对刚上线的机器人会弹错误提示，反而误导运维。

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
async def ws_robots(ws: WebSocket, token: str | None = Query(default=None)):
    """实时流。鉴权走 query 参数 `?token=<JWT>`。

    为什么是 query 而不是头：浏览器 `WebSocket` 构造函数**无法自定义请求头**
    （`new WebSocket(url, protocols)` 里没有 headers 位），只能把凭据放 URL。
    代价是 token 会进服务端访问日志 —— 这是标准权衡，故 REST 侧**禁止**这么传
    （见订单 15 禁止项 10），只有 WS 开这个口子。

    拒绝顺序（**这里踩过一个坑，记录在此避免回退**）：
    直觉写法是"先校验、失败就 close"，但 ASGI 语义下 **accept 之前发的
    `websocket.close` 会被协议层翻译成 HTTP 403 拒绝握手**，客户端拿到的是
    `InvalidStatusCode: HTTP 403`，而不是订单要求的 close code 1008
    （本机实测 websockets 12.0 客户端：`CLOSED:1008` 拿不到，只有 HTTP 403）。
    因此顺序必须是：**先 `accept()`，紧接着 `close(1008)`，中间不 send 任何业务帧**。
    这样既满足"以 1008 关闭"，又满足"不得先发 snapshot" —— 两者并不矛盾，
    矛盾的是"accept 前 close"。
    """
    sub = await authenticate_ws_token(token)
    await ws.accept()
    if sub is None:
        # 1008 = policy violation（RFC 6455 §7.4.1），前端据此区分"被拒"与"网络断"。
        # ⚠️ 此处与下一条语句之间**禁止**插入任何 send_json / send_text：
        # 那是未授权者拿到 snapshot 的唯一窗口。
        await ws.close(code=1008)
        log.warning("ws rejected: missing or invalid token")
        return

    # 订单 16 T1b：连接计数在**鉴权通过之后**才 +1。
    # 若放在 accept 之后、鉴权之前，被 1008 拒掉的握手也会计入，
    # 于是"1008 风暴"（前端带着过期 token 反复重连）会表现为连接数虚高，
    # 掩盖真正的"大屏到底开着几台"这个信号。
    global _ws_active
    _ws_active += 1
    log.info("ws connected", extra={"ws_active": _ws_active})

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
        # 配平上面的 +1。必须与计数点成对出现在 finally 里：
        # 漏掉它的话，客户端异常断开（浏览器崩溃/网络切换）会让计数只增不减，
        # 面板上"连接数 47"而实际只有 3 台大屏 —— 假数据比没数据更糟。
        _ws_active -= 1
        log.info("ws disconnected", extra={"ws_active": _ws_active})
