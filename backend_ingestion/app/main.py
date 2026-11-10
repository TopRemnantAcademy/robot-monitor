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
from app.db.redis import close_redis
from app.db.session import close_pool, ping
from app.logging_json import configure_json_logging
from app.schemas.api import (
    HealthResponse,
    RobotHistoryResponse,
    RobotListResponse,
    SupportedRobotsResponse,
)
from app.catalog import lookup
from app.security_middleware import install_cors, install_rate_limit
from app.services import liveness, mqtt_subscriber, state_cache
from app.services.batch_writer import BatchWriter
from app.services.metadata_writer import metadata_writer
from app.services.offline_buffer import DurableBuffer, run_replay
from app import ws_probe
from app.ws_backpressure import WsOutbound
from app.ws_hub import hub as ws_hub

# 订单 16 T4：单行 JSON 日志，供订单 07 的 openobserve/loki 直接采集。
# 必须在**其它 logger 首次输出之前**配置 —— uvicorn 的启动日志会经由根 logger，
# 晚配的话前几行仍是文本，采集侧就会看到混合格式。
configure_json_logging(logging.INFO)
log = logging.getLogger("main")

writer = BatchWriter()

# R-12：`/history` 的跨度路由与上限（history() 专用，见 infra/init.sql §5.1b）。
# 大小阈值：> 3h 走小时桶。取 180 而非 1440（24h）是因为 3h×10s/桶 ≈ 1080 点/台时
# 前端折线已接近可读上限；再往上必须下采样，晚切不如早切。
HOURILY_AFTER_MINUTES = 180
# 上限 7 天 = 10080 分钟。这是**防护栏**而非精确容量规划：`minutes` 原无上限，
# 一次 `?minutes=525600`（一年）足以在原始表上做全表扫描把连接池拖满。
# 7d 与 30 天原始保留（§6.2）留出余量，也覆盖「一周趋势」这一真实运维诉求。
MAX_HISTORY_MINUTES = 7 * 24 * 60  # 10080


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    writer_task = asyncio.create_task(writer.run())
    # robot_metadata 心跳（G-6 / ISSUE #96）：独立于 batch_writer 的旁路线，按 1s
    # 折叠 + 单语句批量 UPDATE。它与 writer 的守护方式一致 —— 都是「启动即常驻、
    # 关闭前显式冲刷」。开关关闭时 run() 立刻 return，任务自灭。
    metadata_task = asyncio.create_task(metadata_writer.run())
    mqtt_task = asyncio.create_task(mqtt_subscriber.run(writer))
    # WS 合成探针：客户端视角验活（缺口 G-3）。它只读指标，不碰数据流；
    # 关闭时 `run()` 立刻 return，任务自灭，无需在 shutdown 里特判。
    probe_task = asyncio.create_task(ws_probe.run())
    # 오프라인 버퍼 재생 루프（Benchmark C: store-and-forward）. DB 장애로 버퍼링된
    # 행을 DB 복구 시 재투입. Redis 연결은 lazy(실제 사용 시까지 연결 안 함),
    # 버퍼/재생은 모두 실패에 안전(정상 데이터 손실 없음). 꺼져 있으면 태스크 미생성.
    offline_task = None
    if settings.offline_buffer_enabled:
        from app.db.redis import get_redis

        _buffer = DurableBuffer(
            await get_redis(), settings.offline_buffer_key, settings.offline_buffer_maxlen
        )
        offline_task = asyncio.create_task(
            run_replay(writer, _buffer, settings.offline_buffer_replay_interval_s)
        )
    # WS 出站中枢（ISSUE #71）：全进程唯一的 Redis pubsub 订阅 + 100ms 合并节拍。
    # 必须先于第一个 WS 连接 start()，否则早期连接扇不到增量帧。
    await ws_hub.start()
    app.state.writer_task = writer_task
    app.state.metadata_task = metadata_task
    app.state.mqtt_task = mqtt_task
    app.state.probe_task = probe_task
    app.state.offline_task = offline_task
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
    #   ③ 再冲刷 metadata 心跳存量。必须排在 close_pool() **之前**：stop() 只是
    #      置位并唤醒 run()，真正的 flush() 要等 run() 跑完，此时池还必须活着。
    await metadata_writer.stop()
    await asyncio.gather(metadata_task, return_exceptions=True)
    #   ④ 探针只是旁观者，没有存量要冲刷，直接取消（它 slept 在 interval 上）。
    probe_task.cancel()
    await asyncio.gather(probe_task, return_exceptions=True)
    #   ④b 오프라인 버퍼 재생 루프 취소（버퍼/재생은 실패에 안전하므로 즉시 취소 ok）.
    if offline_task is not None:
        offline_task.cancel()
        await asyncio.gather(offline_task, return_exceptions=True)
    #   ⑤ WS 中枢：先停扇出与共享订阅，再清注册表 —— 必须在 close_redis 之前，
    #      否则 _consume_loop 会对着已关的 Redis 退避重连、日志刷屏。
    await ws_hub.stop()
    await close_pool()
    await close_redis()
    # auth 的 ORM engine 是独立于 asyncpg 业务池的（fastapi-users 要 SQLAlchemy 会话），
    # 同样要在收尾时释放，否则容器退出时连接会挂到服务端超时为止。
    await close_engine()
    # 最后再收尾 trace：BatchSpanProcessor 队列里可能还压着关闭阶段的 span。
    telemetry_tracing.shutdown_tracing()
    log.info("backend stopped")


app = FastAPI(title="RobotMonitor Ingestion", lifespan=lifespan)

# R-17：CORS + 可选限流（中间件挂载，**不改任何路由处理函数/契约/鉴权**）。
# 必须在第一个请求到达前挂载 —— Starlette 的 add_middleware 只在应用启动前有效。
# 两者都默认"关闭/最小暴露"，未配置 env 时行为与改动前**逐字一致**：
#   - CORS_ALLOW_ORIGINS 为空   → 不挂 CORS 中间件（同源部署够用）
#   - RATE_LIMIT_ENABLED=false  → 不挂限流中间件
# 挂载顺序无相互依赖（一个管响应头、一个管请求计数），但都在 include_router 之前，
# 保证对**全部**路由（含 /auth/* 登录端点）生效。
install_cors(app, settings.cors_allow_origins)
install_rate_limit(
    app,
    enabled=settings.rate_limit_enabled,
    per_minute=settings.rate_limit_per_minute,
)

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
    # 在线数（ISSUE #77）在这里顺带刷新：`/healthz` 本来就是编排系统以秒级频率打的探针，
    # 借它的节奏维护 Gauge，就不必再为 Prometheus 单独起一个定时器（拉模型下
    # 没人 set 的 Gauge 会永远停在初值，正是"常量 0"的假绿来源）。
    online = await liveness.online_count()
    metrics.robots_online.set(online)
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "up" if db_ok else "down",
        "written": writer.written,
        "flushes": writer.flushes,
        "retries": writer.retries,
        "rejected": writer.rejected,
        "buffered": writer.buffered,
        "queue_depth": writer.queue.qsize(),
        "mqtt_received": mqtt_subscriber.received,
        "mqtt_dropped": mqtt_subscriber.dropped,
        # 丢行三元组（ISSUE #19 G2）：submitted 是交给落库的行数，
        # written 是真正入库的，skipped 是 ON CONFLICT DO NOTHING 静默丢弃的。
        # 三者与 rejected（降级链判定不可写的坏行）合起来才是"收到多少 / 入库多少"的完整答案。
        "rows_submitted": writer.rows_submitted,
        "rows_written": writer.written,
        "rows_skipped": writer.rows_skipped,
        # robot_metadata 心跳（G-6 / ISSUE #96）：flushes 是刷新轮数，rows_written
        # 是被 UPDATE 命中的台数，deduped 是被折叠掉的重复条数 —— 100 台 10Hz
        # 稳态下 deduped/flushes ≈ 900，是「批量语义确实生效」的直接读数。
        "metadata": metadata_writer.stats,
    }


# 当前活跃 WS 连接数（订单 16 T1b 的 `robot_backend_ws_connections`，ISSUE #72 改名）。
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
    # 订单 16 验收要求队列水位压测时有真实波动（非常量 0），正是靠这一次 set ——
    # 少了它，Gauge 初值是 0 且永不变。ISSUE #72 后仅剩这一套 Gauge（无重复刷新）。
    metrics.refresh_gauges(writer.queue.qsize(), _ws_active)
    body, ctype = metrics.render()
    # ctype 由 prometheus_client 给出（`text/plain; version=0.0.4; charset=utf-8`），
    # 必须原样透传 —— 版本号是 Prometheus 解析器协商格式的依据，不能丢。
    return PlainTextResponse(content=body, media_type=ctype)


@app.get("/robots", response_model=RobotListResponse)
async def list_robots(_user: RobotUser = Depends(current_active_user)):
    """100 台最新状态。只读端点：viewer 起可访问，返回体与加鉴权前逐字段一致。

    ISSUE #77：返回体新增 `is_online` / `last_seen` 两个可选字段，并**恒定返回 100 台**
    （停报的机器人回落为离线占位，而不是从列表里消失）。
    """
    return {"robots": await state_cache.snapshot(settings.robot_ids)}


@app.get("/supported-robots", response_model=SupportedRobotsResponse)
async def supported_robots(_user: RobotUser = Depends(current_active_user)):
    """平台支持的机型目录 + 在册机群构成。

    「平台支持哪些机型」的**事实来源**是 `robot_metadata.model_type` 的去重值（DB 驱动，
    零幻觉）；展示元数据（名称/描述/自由度/形态）由 `app.catalog.lookup()` 补充，
    未登记的机型回退占位、绝不编造。viewer 起可读（这是「平台能力」的公开说明，
    不含任何写操作）。

    返回体同时给出每个机型的在册总数 / 在线数 / 固件版本 / 工位分布 ——
    UI 用它渲染「支持的机型」卡片，admin UI 额外展示机群管理明细。
    """
    from app.db.session import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT model_type,
                   COUNT(*)                                                   AS total,
                   COUNT(*) FILTER (WHERE is_online)                        AS online,
                   array_remove(
                       array_agg(DISTINCT firmware_version)
                       FILTER (WHERE firmware_version IS NOT NULL), NULL
                   )                                                         AS firmwares,
                   COUNT(DISTINCT assigned_station)
                       FILTER (WHERE assigned_station IS NOT NULL)           AS stations
            FROM robot_metadata
            GROUP BY model_type
            ORDER BY total DESC, model_type
            """
        )

    models = []
    total_robots = 0
    for r in rows:
        meta = lookup(r["model_type"])
        total_robots += r["total"]
        models.append(
            SupportedRobotOut(
                model_type=r["model_type"],
                display_name=meta.display_name,
                form_factor=meta.form_factor,
                dof=meta.dof,
                vendor=meta.vendor,
                description=meta.description,
                generic=meta.generic,
                total=r["total"],
                online=r["online"],
                firmware_versions=list(r["firmwares"] or []),
                stations=r["stations"],
            )
        )
    return {"models": models, "total_robots": total_robots}


@app.get("/robots/{robot_id}/history", response_model=RobotHistoryResponse)
async def history(
    robot_id: str,
    minutes: int = 5,
    resolution: str = "10s",
    _user: RobotUser = Depends(current_active_user),
):
    """最近 N 分钟历史（供 Drawer 折线 + 关节热力图）。

    数据源（ISSUE #76 起）：**优先查连续聚合 `robot_history_10s`**，
    该视图的刷新策略是 end_offset=10s / schedule=30s，延迟在数十秒内
    （见 infra/init.sql §5.1），满足 Drawer 对「最近数据」的要求。

    为什么不再直查原始表：
      原实现对 `robot_telemetry` 做 `time_bucket`，过滤 `time >= now() - 5min`。
      100 台 × 10Hz = 1000 行/s ⇒ 5 分钟窗口内单台约 3000 行、全量扫描更多；
      每次 Drawer 打开都要在原始 hypertable 上区间扫描 + 逐行 last() 聚合。
      改查连续聚合后，同一查询只读「已预聚合的 ~30 行/台」，扫描量下降三个数量级
      —— 这是真快，不是靠 Histogram 观测出来的假快。

    为什么保留原始表回退：
      连续聚合在 `REFRESH` 之前是空的（`WITH NO DATA` + 策略异步刷新），
      冷启动、策略 job 未跑到、或刷新窗口尚未覆盖时，直查会返回空数组 →
      Drawer 曲线恒空。回退保证「聚合未就绪 ≠ 端点失效」，
      语义与 spec/00 §5.3「聚合未就绪时回退原始表」逐字一致。

    `last(joint_positions, time)` 在两个分支中都**必须保留** ——
    它是 Drawer 第三条折线「角度(rad)」的唯一数据源
    （RobotDetailDrawer.tsx:88），缺了它曲线恒为 null（订单 16 踩过的坑）。

    跨度路由（R-12）：`minutes <= HOURILY_AFTER_MINUTES(180)` 仍走 10s 聚合，
    行为与 ISSUE #76 逐字一致（R-03 §4 契约硬红线）；超过则改走小时级聚合
    `robot_history_1h`（infra/init.sql §5.1b），把 24h 窗口从 8640 点/台压到 24 点/台。
    小时路径的 `resolution` 回显 `3600s`（真实桶秒数，不是入参 `10s`）——
    `resolution` 是「响应里每点代表多少秒」的事实标签，回显入参会让前端把 1h 桶当 10s 桶。
    **不引入 `resolution=1h`**：`_parse_resolution` 是 R-03 §4.1 锁定的整数秒语义，
    小时下采样由 `minutes` 阈值触发，与 `resolution` 参数正交。
    """
    try:
        res_s = _parse_resolution(resolution)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid resolution: {resolution}")

    # 上限保护（R-12 T2）：必须在**取连接之前**拒绝，否则一次超大跨度查询已经
    # 把全表扫描甩给了 DB 才报 400 —— 防护栏就失去了意义。
    # 放在 `_parse_resolution` 之外：那是对 `resolution` 的校验（R-03 §4.1），
    # 本条是对 `minutes` 的校验，两者职责不同，混在一起会动摇 R-03 的锁定面。
    if minutes > MAX_HISTORY_MINUTES:
        raise HTTPException(status_code=400, detail="history span too large")

    span_s = max(1, minutes) * 60
    # 跨度路由（R-12）：
    #   小跨度 → 10s 聚合（`sql_cagg`，行为与 ISSUE #76 逐字一致，R-03 回归红线）；
    #   大跨度 → 小时聚合（`sql_cagg` 换成 robot_history_1h，其余模板不变）。
    # 只切 `source`，bucket 表达式仍取物化列 `bucket` —— 1h 视图的物化列就叫 bucket，
    # 与 10s 视图同构，故无需按桶宽再套一次 time_bucket（那会掩盖自动建的索引）。
    hourly = minutes > HOURILY_AFTER_MINUTES
    agg_source = "robot_history_1h" if hourly else "robot_history_10s"
    # 响应 resolution 回显**实际**桶秒数：小时路径 3600，小路径沿用入参 res_s。
    out_res_s = 3600 if hourly else res_s

    # 两个分支的 SELECT 列**逐字对齐**（列名即响应字段名），
    # 这样结果映射只写一份，不会出现「聚合加了列、回退漏了列」的漂移。
    # 差别只在 FROM / WHERE：聚合表按 `bucket` 过滤，原始表按 `time` 过滤。
    _select = """
        SELECT {bucket}                          AS bucket,
               robot_id,
               last(status_code, time)           AS status_code,
               last(battery_pct, time)           AS battery_pct,
               max(max_joint_temp)               AS max_joint_temp,
               last(joint_temperatures, time)    AS joint_temperatures,
               last(joint_torques, time)         AS joint_torques,
               -- joint_positions 必须返回：前端 Drawer 的第三条折线「角度(rad)」
               -- 数据源就是 point.joint_positions[joint]（RobotDetailDrawer.tsx:88）。
               -- 缺失时该曲线恒为 null，图上永远空白。
               last(joint_positions, time)       AS joint_positions
        FROM {source}
        WHERE robot_id = $2
          AND {time_col} >= now() - (($3 || ' seconds')::interval)
        GROUP BY bucket, robot_id
        ORDER BY bucket
    """
    sql_cagg = _select.format(
        bucket="bucket", time_col="bucket", source=agg_source
    )
    sql_raw = _select.format(
        bucket="time_bucket(($1 || ' seconds')::interval, time)",
        time_col="time",
        source="robot_telemetry",
    )

    from app.db.session import get_pool

    pool = await get_pool()
    # 订单 16 T1b：/history 耗时直方图。
    # 计时**只覆盖 DB 查询**（含 acquire），不含参数校验与结果序列化 ——
    # 前者是数据库问题、后者是 CPU 问题，混在一个桶里会让"该不该建连续聚合表"
    # 这个决策失去依据（序列化耗时不随数据量线性增长，查询耗时才会）。
    # try/finally 而非只在成功时 observe：**查询失败的那一刻往往正是最慢的一次**
    # （超时/锁等待），漏掉它会让 p99 被系统性低估。
    _t0 = time.perf_counter()
    # source 只在**查询成功返回**后才计数，故初值 None 而非 "cagg" ——
    # 若在异常路径上也记成 cagg，会把「聚合查询失败」混进「聚合命中」的分子里，
    # 恰好把最该暴露的故障掩盖成最理想的状态。
    source: str | None = None
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql_cagg, str(res_s), robot_id, str(span_s))
            if not rows:
                # 聚合未就绪（策略尚未刷新到该窗口）→ 回退原始表。
                # 只在**空结果**时回退，不无条件双查：非空路径不该付第二遍代价，
                # 否则改连续聚合的收益会被自己抵消掉。
                source = "raw"
                rows = await conn.fetch(sql_raw, str(res_s), robot_id, str(span_s))
            else:
                source = "cagg"
    except Exception as exc:
        log.exception("history query failed")
        return JSONResponse(status_code=503, content={"detail": f"query failed: {exc}"})
    finally:
        metrics.history_latency_seconds.observe(time.perf_counter() - _t0)
        # 数据源标签：p95 下降到底是「真变快」还是「查了别的表」，靠这个区分。
        # 异常路径 source 仍为 None，不计入任一 series（失败由 `m` 之外的方式暴露：
        # 503 + log.exception），避免把失败伪装成命中。
        if source is not None:
            metrics.history_source.labels(source=source).inc()

    # 注意：这里**刻意不校验 robot_metadata**。init.sql 预置了 100 台机器人，
    # 但「从未上报过数据的机器人」查历史必然是空数组——这是正常语义而非错误。
    # 若改成 404，Drawer 对刚上线的机器人会弹错误提示，反而误导运维。
    #
    # 同理（ISSUE #77）：**不能**用「history 为空」反推 offline —— 停报的机器人
    # 历史里是有折线的，它只是"最近这段没有新点"。在线态的唯一事实源是
    # `/robots` 与 WS snapshot 的 `is_online`，本端点不参与判定。

    return {
        "robot_id": robot_id,
        "resolution": f"{out_res_s}s",
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
    """'10s' / '30s' / '1m' → 整数秒。非整数倍（'2.5s' / '1.5m'）**拒绝**，不静默截断。

    Issue #74：旧实现直接 `int(float(...))`，把 '2.5s' 静默变成 2 秒、
    '1.5m' 变成 90 秒、'0.5m' 变成 30 秒 —— 用户写的分辨率与实际查询分辨率
    不一致，且**没有任何错误提示**。这类"差一点就对"的静默降级在运维看板上最难查：
    图上点数与预期对不上，但 HTTP 200、无告警。

    因此这里改为：**只有当值恰好是整数时才接受**，否则 `raise ValueError`，
    由 `history()`（`main.py:227-229`）转成 HTTP 400 + `invalid resolution: <原文>`。
    合法整数值（'10s'→10、'30s'→30、'1m'→60）行为**逐字不变**；
    下限仍为 1s（sub-1s 仍禁止，避免压垮查询）。

    【为什么不用 'm' 先乘 60 再判整】
    '0.02m' 乘 60 得 1.2 秒，虽 >1 却不是整秒，对 `time_bucket('1.2 seconds')`
    这种非整秒桶没有意义；先判「分钟数本身是整数」语义更清晰，
    也避免 '1.6666666666666667m' ≈ 100s 这种"碰巧凑近整数"的模糊输入被放行。
    """
    s = resolution.strip().lower()
    if s.endswith("ms"):
        raise ValueError("resolution below 1s not supported")
    unit = s[-1:] if s[-1:] in ("s", "m") else ""
    if not unit:
        raise ValueError(resolution)

    raw = s[:-1]
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(resolution) from None

    # 拒绝 nan / inf：`float('nan')` 不会被上面的 except 拦住（它解析成功），
    # 而 `nan != int(nan)` 恒真、`int(nan)` 直接抛 ValueError —— 显式挡掉，
    # 让错误信息稳定为 "invalid resolution"，而不是随机冒出的 math domain error。
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(resolution)

    # 非整数倍（含 '2.5s' / '1.5m' / '0.5m'）→ 400，不再静默截断。
    if value != int(value):
        raise ValueError(f"resolution must be a whole number of seconds: {resolution}")

    seconds = int(value) * (60 if unit == "m" else 1)
    # 下限 1s（禁止项：不得放宽）。'0.5m'=30 合法，但 '0m'/'0s' 仍拒绝。
    if seconds < 1:
        raise ValueError("resolution below 1s not supported")
    return seconds


def _as_dict(v) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return {}
    return {}


async def _ws_watchdog(ws: WebSocket, outbound: WsOutbound) -> None:
    """慢客户端/半开连接的主动摘除（ISSUE #73）。

    两条判死依据，任一命中即 `close`：

    1. **出站队列持续满**：说明客户端消费速度长期低于 1000 msg/s 的流入速度。
       偶尔满一下是正常的（网络抖动被队列吸收），持续满等于"这条连接已经跟不上"，
       继续挂着只会让队列反复丢帧、日志刷屏。
    2. **发送持续阻塞**：`last_sent_at` 长时间不前进 = 发送协程卡在 `send_json`
       上 = TCP 写缓冲满了但内核没报错（典型的"客户端拔网线"半开连接）。
       TCP 自己发现这种状态可能要好几分钟，`_ws_active` 就会虚高到那时。

    为什么用独立的 watchdog 而不是复用心跳：心跳只证明"队列还塞得进去"，
    它**不读**客户端是否还在消费（服务端发得出去≠客户端收得到）。
    两者一起才覆盖了 ISSUE #73 验收里"慢客户端"与"断网客户端"两种形态。
    """
    timeout = settings.ws_outbound_close_timeout_s
    # 用 event loop 单调时钟：墙钟会被 NTP 跳变，超时判定不能受它影响。
    loop = asyncio.get_running_loop()
    full_since: float | None = None
    last_sent_marker = 0

    while True:
        await asyncio.sleep(settings.ws_heartbeat_interval_s)
        now = loop.time()

        # 心跳哨兵：让发送协程有机会在"没有可见遥测"时也验一次活。
        outbound.heartbeat()
        metrics.outbound_queue_depth.set(outbound.qsize)

        # 判据 1：队列持续满
        if outbound.is_saturated:
            if full_since is None:
                full_since = now
            elif now - full_since >= timeout:
                log.warning(
                    "ws slow client evicted: outbound queue full for %.0fs", now - full_since
                )
                metrics.ws_slow_client_evicted.inc()
                await _ws_try_close(ws, code=1001, reason="slow consumer")
                return
        else:
            full_since = None

        # 判据 2：发送协程长时间没有推进
        if outbound.sent_count == last_sent_marker:
            if outbound.last_sent_at is not None and now - outbound.last_sent_at >= timeout:
                log.warning("ws half-open client evicted: no successful send for %.0fs", timeout)
                metrics.ws_slow_client_evicted.inc()
                await _ws_try_close(ws, code=1001, reason="heartbeat timeout")
                return
        else:
            last_sent_marker = outbound.sent_count


async def _ws_watch_disconnect(ws: WebSocket) -> None:
    """等到客户端真的断开为止，然后返回（供主循环 `asyncio.wait` 赛跑）。

    【为什么必须显式读 WebSocket】
    改前这个端点只**出站**（await pubsub.listen → send_json），从不调用 receive。
    于是客户端走了服务端也不知道：TCP 半开时内核不报错，`_ws_active` 会一直虚高，
    与订单 16 防"假绿"的初衷相悖（ISSUE #73 根因第二条）。这里 `await ws.receive()`
    让 Starlette 把 FIN/RST 或客户端 close 帧翻译成异常/返回，从而即时感知。

    【为什么不像 ws_probe 那样回 pong】
    Starlette 在 `receive()` 层**原生**处理 `"ping"`：客户端发文本 `ping`，
    它会自动回 `pong`（`starlette/websockets.py` 的 receive_text 分支）。
    本函数只需消费消息、不解析内容 —— 解析是应用层自己的事，
    而契约里客户端并不发业务上行帧（WS 是单向下行流，见 spec/00 §5.2）。
    """
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return


async def _ws_try_close(ws: WebSocket, *, code: int, reason: str) -> None:
    """尽力关闭连接。已经断开时静默 —— 关闭动作与 `finally` 回收是两条独立路径，
    这里失败不代表回收会失败（不回抛，避免把 watchdog 变成未处理的任务异常）。"""
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        log.debug("ws close during eviction failed (already gone)", exc_info=True)


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

    # ① 首帧：100 台全量快照
    try:
        snap = await state_cache.snapshot(settings.robot_ids)
        await ws.send_json({"type": "snapshot", "data": {"robots": snap}})
    except Exception:
        log.exception("snapshot failed")

    # ② 增量：遥测 + 告警（alert 通道是前端告警流的唯一数据源）
    #
    # ISSUE #71 + #73 融合架构：本连接创建**自己的**有界出站通道（#73），
    # 然后注册到进程级 WS 中枢（#71）—— 中枢持有全进程唯一的 Redis 订阅，
    # 按 100ms 节拍把合并后的帧 enqueue_raw 进各连接的队列。
    # 本协程自此只做两件事：注册中枢 + 等连接断开。
    # 改前这里是「每连接自己订阅 chan_stream_all/chan_alerts + 逐条 send_json」，
    # N 个大屏 = N 份 Redis 订阅 + N × 1000 msg/s 的序列化/发送，线性膨胀。
    outbound = WsOutbound(
        ws,
        maxsize=settings.ws_outbound_queue_size,
        alert_slack=settings.ws_alert_overflow_slack,
    )
    outbound.start()

    # 断开侦测（ISSUE #73：不能只靠"发得出去"判断连接还在）。
    # 中枢扇出只入队不读 WebSocket，必须有专职任务 `await ws.receive()`：
    # 客户端正常关闭、崩溃、拔网线（走 RST/FIN）都会让 receive 抛
    # WebSocketDisconnect，从而立刻进 finally。
    disconnect = asyncio.create_task(_ws_watch_disconnect(ws))

    # 背压/心跳超时守卫：队列**持续满**或发送**持续阻塞**超过阈值即主动摘连接。
    # 这是"慢客户端断网后 _ws_active 在心跳超时内归位"的实现（ISSUE #73 验收第三条）：
    # 不能指望 TCP 自己发现半开连接（可能几分钟），必须主动判死。
    watchdog = asyncio.create_task(_ws_watchdog(ws, outbound))

    # 注册动作受 try/finally 保护：注册成功到注销之间的任何退出路径，
    # finally 里的 unregister 都必须执行 —— 否则中枢会向已关闭的连接继续
    # 入队（队列有界不会泄漏内存，但 client_count 会虚高）。
    try:
        await ws_hub.register(outbound)
        # 等客户端断开（watcher 内部 receive 到 disconnect 即返回）。
        # 中枢断连（Redis 掉线）不再影响单条连接：_consume_loop 自带退避重连，
        # 连接只是暂时收不到增量帧 —— 比旧实现"Redis 一抖全部连接退出重连"更稳。
        await disconnect
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws error")
    finally:
        # 【这一整段的写法是被 bug 逼出来的，别改成"顺手的样子"】
        #
        # 本函数可能因**任务被 cancel** 而进 finally（uvicorn 关停、TestClient
        # 上下文退出都会）。任务已处于取消态时，finally 里**任何未加保护的 await
        # 都可能立刻抛出 CancelledError 并中断整个 finally 块** ——
        # 实测最小复现：
        #     try: await sleep(999)
        #     finally: await x(); n -= 1      # x() 抛错 → n -= 1 永不执行
        # 若照直觉写在最后面（本文件第一版就是），一旦 `outbound.aclose()` 抛错，
        # `_ws_active -= 1` 就被跳过，计数只增不减 —— 正是本单要修的那个"假数据"。
        #
        # 因此规则是：**清理动作逐个 try 包住，计数配平放最前且不依赖任何清理成功**。
        # 回收计数是硬承诺（ISSUE #73 验收第三条），不能让它有机会被跳过。

        # ① 先配平计数。此处没有任何 await，因此**不可能**被取消中断。
        #    必须与上面的 +1 成对：漏掉它的话，客户端异常断开（浏览器崩溃/网络切换）
        #    会让计数只增不减，面板上"连接数 47"而实际只有 3 台大屏 ——
        #    假数据比没数据更糟。
        _ws_active -= 1
        log.info("ws disconnected", extra={"ws_active": _ws_active})

        # ② 把本轮连接的背压事实记进指标（同步，无 await，同样不会被中断）。
        metrics.telemetry_frames_dropped.inc(outbound.telemetry_dropped)
        metrics.alerts_deferred.inc(outbound.alerts_deferred)
        if outbound.telemetry_dropped or outbound.alerts_deferred:
            log.warning(
                "ws backpressure on close: telemetry_dropped=%d alerts_deferred=%d",
                outbound.telemetry_dropped,
                outbound.alerts_deferred,
            )

        # ③ 从中枢注销（尽快：下一节拍起不再向本连接入队）。
        #    每个 await 单独 try，任一失败都不影响其它清理 与①的计数。
        try:
            await ws_hub.unregister(outbound)
        except BaseException:  # noqa: BLE001 —— 含 CancelledError，见本段头注释
            log.debug("ws hub unregister failed during teardown", exc_info=True)

        # ④ 停任务、停发送协程。顺序：先停发送协程相关的任务，再关出站 ——
        #    每个 await 单独 try，任一失败都不影响其它清理 与①的计数。
        watchdog.cancel()
        disconnect.cancel()
        for task in (watchdog, disconnect):
            try:
                await asyncio.gather(task, return_exceptions=True)
            except BaseException:  # noqa: BLE001 —— 含 CancelledError，见本段头注释
                pass
        try:
            await outbound.aclose()
        except BaseException:  # noqa: BLE001
            log.debug("ws outbound close failed during teardown", exc_info=True)
