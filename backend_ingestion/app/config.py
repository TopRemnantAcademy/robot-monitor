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
