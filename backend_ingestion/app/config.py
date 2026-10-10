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

    # ---- Redis 频道（spec/00 §5.2/§5.4）----
    chan_stream_all: str = "robot:stream:all"
    chan_stream_one: str = "robot:stream:{robot_id}"
    chan_alerts: str = "robot:alerts"

    @property
    def robot_ids(self) -> list[str]:
        """100 台机器人 ID（与 init.sql 预置的 robot_metadata 一致）。"""
        return [f"humanoid-{i:03d}" for i in range(1, 101)]


settings = Settings()
