"""告警引擎配置（阈值与窗口全部集中在此，spec/02 §3）。"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="../.env", env_file_encoding="utf-8", extra="ignore"
    )

    redis_url: str = "redis://localhost:6379"
    database_url: str = "postgresql://postgres:postgres@localhost:5432/robot_monitor"

    # ---- Redis 频道（与后端约定一致）----
    chan_stream_all: str = "robot:stream:all"
    chan_alerts: str = "robot:alerts"

    # ---- motor_temp（spec/02：>75°C 且持续 ≥3 采样，severity 3，去重 60s）----
    motor_temp_c: float = 75.0
    motor_temp_min_samples: int = 3
    motor_temp_dedup_s: int = 60

    # ---- battery_low（<15%，severity 2，去重 120s）----
    battery_low_pct: float = 15.0
    battery_low_dedup_s: int = 120

    # ---- offline（无遥测 >10s，severity 3，去重 30s）----
    offline_after_s: float = 10.0
    offline_dedup_s: int = 30

    # ---- joint_stuck（5s 内角度方差≈0 但扭矩超阈值，severity 2，去重 60s）----
    joint_stuck_window_s: float = 5.0
    joint_stuck_variance: float = 1e-6
    joint_stuck_torque: float = 8.0
    joint_stuck_dedup_s: int = 60

    # ---- 抑制（inhibition，对照 Alertmanager 的 inhibit_rules）----
    # 机器人已判离线（severity 3）时，抑制同一 robot_id 的其他告警：
    # 机器都离线了，「电量低」「关节卡死」没有处置价值，根因就是离线。
    # 留开关便于回滚与 A/B 对比；关闭后行为回到加抑制之前。
    inhibit_offline: bool = True

    # ---- 判定循环 ----
    tick_interval_s: float = 1.0        # 离线 ticker 间隔
    window_maxlen: int = 100            # 每台机器人滑动窗口容量（10Hz → 约 10s）

    # ---- 告警通知（I-14）----
    # 工厂侧机器人 webhook：留空则整条通知链路静默跳过（默认关闭，避免误配刷屏）。
    # URL 含 token，**只从环境变量注入，禁止写进仓库**。
    alert_webhook_url: str = ""
    # dingtalk | wecom | generic（通用 JSON，交工厂自建转发加工）
    alert_webhook_kind: str = "generic"
    # 推送门槛：默认 3 —— 只推 critical，防「低电量风暴」刷屏
    alert_webhook_min_severity: int = 3

    @property
    def robot_ids(self) -> list[str]:
        return [f"humanoid-{i:03d}" for i in range(1, 101)]


settings = Settings()
