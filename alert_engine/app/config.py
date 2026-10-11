"""告警引擎配置（阈值与窗口全部集中在此，spec/02 §3）。"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="../.env", env_file_encoding="utf-8", extra="ignore"
    )

    redis_url: str = "redis://localhost:6379"
    database_url: str = "postgresql://postgres:postgres@localhost:5432/robot_monitor"

    # ---- DB 连接池（订单 17 发现：db.py 一直在读这两个键，config 却没定义）----
    # `app/db.py:get_pool()` 引用 `settings.db_pool_size` / `settings.db_command_timeout`，
    # 但本文件此前**没有**这两个字段 —— 于是任何走到 `get_pool()` 的路径（告警落库
    # `store.save`、metadata 更新、本单的 escalation 扫描）都会抛
    # `AttributeError: 'Settings' object has no attribute 'db_pool_size'`。
    # 这是订单 08 引入 `app/pool.py` 时漏掉的配置项，本单为跑通 escalation 补齐。
    # 命名与 `backend_ingestion/app/config.py` 对齐（db_pool_min_size 等），
    # 但 pool.py 的入参名是 `pool_size`，故此处取 `db_pool_size`。
    db_pool_size: int = 2          # 预建条数 = 空闲地板（见 app/pool.py:MAX_POOL_SIZE）
    db_command_timeout: float = 30.0

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

    # ---- 升级（escalation，对齐 Alertmanager 的 repeat_interval）----
    # severity=3 且超过该秒数仍未 ack → 置 escalated=TRUE 并重发一次。
    # 300s（5 分钟）取自派工单 T2；这是一个"值班员该响应了"的经验量级，
    # 比它短会催得太急（现场巡检一次就可能超 1 分钟），长则失去催办意义。
    escalate_after_s: int = 300
    # 升级扫描周期。30s 是"及时性 vs 扫描开销"的折中：
    #   - 300s 的阈值下，30s 抖动最多让升级晚 30s 发生，对催办无实质影响；
    #   - 谓词被部分索引 idx_alert_escalation 覆盖（init.sql:206），
    #     每次扫描扫的是窄集，30s 一次开销可忽略。
    escalate_scan_interval_s: float = 30.0
    # 单条告警最多升级重发几次。防止"没人管 → 永远每 5 分钟重发"变成
    # 长期噪声源（Alertmanager 也需配 repeat_interval 而非无限重发）。
    escalate_max_repeat: int = 3

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

    # ---- Prometheus ----
    # 告警引擎不是 FastAPI 应用，没有 HTTP 路由可挂 /metrics，
    # 故由 app/metrics.py:start() 在独立端口起一个 http.server（顺序 13 接线）。
    # 与后端 ingestion 服务端口分离，避免抓取目标冲突。
    metrics_port: int = 8001

    @property
    def robot_ids(self) -> list[str]:
        return [f"humanoid-{i:03d}" for i in range(1, 101)]


settings = Settings()
