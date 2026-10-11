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

    # ---- 认证 / RBAC（订单 15）----
    # JWT 签名密钥。**必须**由 .env 注入，不设默认值 —— 有空默认值等于库里带着
    # 一个人人可签的密钥，任何拿到源码的人都能伪造 admin token（AGENTS.md §5）。
    # 未配置时 AppSettings 构造即抛错，宁可起不来也不要静默降级成"谁都能进"。
    jwt_secret: str
    # token 有效期：8 小时 = 一个班次。大屏是 7×24 公共终端，
    # 过期后由前端 401 拦截清 token 回登录页，由当班人重新登录。
    jwt_lifetime_seconds: int = 8 * 3600

    # ---- Redis 频道（spec/00 §5.2/§5.4）----
    chan_stream_all: str = "robot:stream:all"
    chan_stream_one: str = "robot:stream:{robot_id}"
    chan_alerts: str = "robot:alerts"

    @property
    def robot_ids(self) -> list[str]:
        """100 台机器人 ID（与 init.sql 预置的 robot_metadata 一致）。"""
        return [f"humanoid-{i:03d}" for i in range(1, 101)]


settings = Settings()
