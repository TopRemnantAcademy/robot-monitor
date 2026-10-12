"""环境变量配置（pydantic-settings）。

对应 spec/00 §6 与 .env.example。所有阈值/窗口集中在此，禁止在业务代码里硬编码。
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- MQTT ----
    mqtt_broker: str = "localhost"
    mqtt_port: int = 1883

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379"

    # ---- TimescaleDB ----
    database_url: str = "postgresql://postgres:postgres@localhost:5432/robot_monitor"

    # ---- 批量落库双重触发（spec/01 §4.2）----
    batch_trigger_count: int = 500
    batch_trigger_seconds: float = 1.0
    batch_queue_maxsize: int = 20000
    # 单批写入失败时的重试与退避（见 services/batch_writer.py 的降级链）
    batch_flush_max_retries: int = 3
    batch_flush_retry_backoff_s: float = 0.5
    # 病态批次隔离的二分深度上限：每层把批减半，段大小 = batch/2^depth。
    # 上限存在的意义是保证最坏情况下的往返次数有界（避免 O(n) 次查询）。
    # 深度 8 + 批 500 → 最小段 1 行，足以定位单行坏数据。
    batch_isolation_max_depth: int = 8

    # ---- asyncpg 连接池 ----
    # 空闲地板条数（asyncpg 0.32 语义，见 app/db/pool.py）。写路径是单连接顺序刷盘，
    # 2 条足矣；`db_pool_max_size` 留出并发余量（/healthz 探活 + 读路径会同时到达）。
    # 注：不采用订单 08 分支那套「DB_POOL_SIZE 同时当 init_size/min_size/max_size」的写法，
    # 三值相等会把池退化成信号量（本机实测见 app/db/pool.py 的 MAX_POOL_SIZE 注释）。
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10
    db_command_timeout_s: float = 30.0
    db_max_idle_s: float = 300.0
    # /healthz 的 DB 探活（就绪探针不应每次打库，缓存结果）
    db_health_cache_s: float = 5.0

    # ---- Redis 最新状态缓存（spec/01 §4.3）----
    state_ttl_seconds: int = 10

    # ---- robot_metadata 心跳（G-6 / ISSUE #96）----
    # 总开关。关闭后 metadata_writer 的 run() 直接退出、flush() 直接返回：
    # 既不改写 robot_metadata，也不做离线扫描，遥测写路径（batch_writer）语义与
    # 吞吐完全不受影响 —— 这正是「不得改变既有遥测写路径」的落地方式。
    metadata_heartbeat_enabled: bool = True
    # metadata 心跳的刷盘周期。语义上必须 ≤ state_ttl_seconds：Redis 里的
    # `robot:state:{id}` 活 10s，若心跳比它慢，会出现「Redis 已判过期、DB 里
    # is_online 还是 true」的窗口期。1s 同时把写放大压到 ≤100 行/秒的量级。
    metadata_flush_seconds: float = 1.0

    # ---- 在线判定阈值（ISSUE #77）----
    # 「现在还活着吗」只由后端判定：前端此前拿 `status_code === 0` 猜在线，
    # 于是停报的机器人把最后一帧的 status_code 冻在那里，永远显示在线。
    #
    # 30s 的依据（不是拍脑袋）：正常上报是 10Hz（100ms 一条），30s = 连续丢 300 帧，
    # 远大于网络抖动/MQTT 重连/JS 事件循环阻塞的量级；而 AGENTS.md 的验收要求
    # 「避免 10Hz 正常抖动误判」。与 alert_engine 的 offline_after_s(10s) **刻意不同**：
    # 那个是**告警阈值**（早报=早处置），这个是**展示阈值**（晚划=不闪）。
    # 两者语义不同，不要为了"看着一致"把它们并成一个值。
    robot_offline_threshold_s: float = 30.0

    # ---- HTTP ----
    ws_host: str = "0.0.0.0"
    ws_port: int = 8000

    # ---- WS 合成探针（缺口 G-3「看板假的绿」）----
    # 探针是**旁观者**：关掉它、或它跑挂，都不影响遥测写入与 WS 广播。
    # 默认开启，因为「没有它就没有客户端视角」；企业内网只需 loopback，无外网依赖。
    ws_probe_enabled: bool = True
    # 探针间隔。下限 30s 被 `ws_probe.py:MIN_INTERVAL_S` 钳住 ——
    # 这里配更小也不会更快（探针自己也是 WS 客户端，会与 1000 msg/s 抢 loopback）。
    ws_probe_interval_s: float = 60.0
    # 探针目标路径。写成配置是为了将来出现「多路 WS」时不必改代码。
    ws_probe_path: str = "/ws/robots"
    # 启动宽限：uvicorn 的 lifespan startup 里就会把探针任务创建出来，
    # 此时端口尚未 accept，立刻探必然失败。给一个窗口避免开局一次假失败。
    ws_probe_startup_grace_s: float = 5.0

    # ---- WS 出站背压 + 心跳（ISSUE #73）----
    # 每条连接一个**有界**出站队列：转发循环只做 put_nowait（永不 await），
    # 慢客户端不再拖住 Redis 转发循环，内存最坏 = 队列长度 × 帧大小（常量）。
    # 依据：100 台 × 10Hz = 1000 msg/s，若逐帧 await 到慢客户端，一个卡死的
    # 浏览器标签就能把整个进程的内存拖无界（见 app/ws_backpressure.py 头注释）。
    #
    # 取值 500：约 0.5s 的遥测（1000 msg/s）—— 足以吸收移动网络的抖动，
    # 又不至于让"丢掉最旧帧"这件事被延迟到用户可见。配大等于回到无界。
    ws_outbound_queue_size: int = 500
    # 心跳哨兵的投放间隔。发送协程每轮循环投一个，它会被 `await send_json`
    # 牵着经过真实的背压/断开判定 —— 于是"投得进去"本身就是一次验活。
    # 浏览器侧的半开连接（拔网线但 TCP 未断）会表现为发送阻塞，
    # 该连接随后被 `ws_outbound_close_timeout_s` 兜底摘除。
    ws_heartbeat_interval_s: float = 15.0
    # 单条连接在队列持续满 / 发送持续阻塞时的最长容忍时间，超时主动 close。
    # 上限存在是为了让"慢客户端断网后计数在心跳超时内归位"成为可验证的承诺，
    # 而不是"等 TCP 自己发现"（可能几分钟）。默认 60s ≈ 3 个心跳周期。
    ws_outbound_close_timeout_s: float = 60.0
    # 出站队列满时，允许为**不可丢的告警**额外扩张的格数（见 ws_backpressure）。
    ws_alert_overflow_slack: int = 64

    # ---- WS 广播合并节拍（ISSUE #71）----
    # 服务端出站中枢的扇出周期。100ms = 10Hz，与上游遥测速率（100 台 × 10Hz）
    # 及前端 100ms 节流（spec/03 §6）同拍：每节拍每台机器人只扇出**最新一帧**，
    # 中间帧在「覆盖写」里被自然合并（ws_hub_coalesced_total 计数即证据）。
    # 调大 → 服务端更省、但看板更新变钝；调小 → 更实时、但序列化/发送次数回升。
    ws_broadcast_interval_ms: float = 100.0

    # ---- 认证 / RBAC（订单 15）----
    # JWT 签名密钥。**必须**由 .env 注入，不设默认值 —— 有空默认值等于库里带着
    # 一个人人可签的密钥，任何拿到源码的人都能伪造 admin token（AGENTS.md §5）。
    # 未配置时 AppSettings 构造即抛错，宁可起不来也不要静默降级成"谁都能进"。
    jwt_secret: str
    # token 有效期：8 小时 = 一个班次。大屏是 7×24 公共终端，
    # 过期后由前端 401 拦截清 token 回登录页，由当班人重新登录。
    jwt_lifetime_seconds: int = 8 * 3600

    # ---- OpenTelemetry（订单 16 T3）----
    # 留空 = **关闭**（默认）。工厂内网常无 OTLP collector，为空即 no-op，
    # 绝不因未配置而启动失败（见 app/telemetry_tracing.py 的分层降级）。
    # 填 `http://collector:4318` 即启用，本模块会补全 /v1/traces 路径。
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "robot_backend"

    # ---- Redis 频道（spec/00 §5.2/§5.4）----
    chan_stream_all: str = "robot:stream:all"
    chan_stream_one: str = "robot:stream:{robot_id}"
    chan_alerts: str = "robot:alerts"

    @property
    def robot_ids(self) -> list[str]:
        """100 台机器人 ID（与 init.sql 预置的 robot_metadata 一致）。"""
        return [f"humanoid-{i:03d}" for i in range(1, 101)]


settings = Settings()
