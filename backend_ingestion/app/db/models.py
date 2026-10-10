"""SQLAlchemy 2.0 ORM 模型（与 infra/init.sql 严格对齐）。

要点：
- robot_telemetry 的复合主键为 (time, robot_id) ——
  TimescaleDB 要求唯一索引必须包含分区列 time，故顺序不可颠倒。
- 三个关节列与 detail 必须是 **JSONB**（init.sql 用的是 JSONB）。
  用 sqlalchemy.JSON 会编译成 `JSON`（非 JSONB）→ ORM 侧 DDL 与库内真实类型不一致，
  元数据漂移且丢失 JSONB 的索引/操作符能力。
  见 models.py:RobotTelemetry.joint_temperatures 的 JSONB 标注。

写路径由 asyncpg 批量完成（见 services/batch_writer.py），此处模型用于读路径与 schema 对齐。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Double, Integer, PrimaryKeyConstraint, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RobotTelemetry(Base):
    __tablename__ = "robot_telemetry"

    time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), primary_key=True)
    robot_id: Mapped[str] = mapped_column(Text, primary_key=True)

    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    battery_pct: Mapped[float] = mapped_column(Double, nullable=False)
    battery_temp: Mapped[float | None] = mapped_column(Double)
    max_joint_temp: Mapped[float] = mapped_column(Double, nullable=False)
    joint_temperatures: Mapped[dict] = mapped_column(JSONB, nullable=False)
    joint_positions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    joint_torques: Mapped[dict] = mapped_column(JSONB, nullable=False)
    pos_x: Mapped[float | None] = mapped_column(Double)
    pos_y: Mapped[float | None] = mapped_column(Double)
    pos_z: Mapped[float | None] = mapped_column(Double)
    orient_w: Mapped[float | None] = mapped_column(Double)
    orient_x: Mapped[float | None] = mapped_column(Double)
    orient_y: Mapped[float | None] = mapped_column(Double)
    orient_z: Mapped[float | None] = mapped_column(Double)


class RobotMetadata(Base):
    __tablename__ = "robot_metadata"

    robot_id: Mapped[str] = mapped_column(Text, primary_key=True)
    model_type: Mapped[str | None] = mapped_column(Text)
    firmware_version: Mapped[str | None] = mapped_column(Text)
    assigned_station: Mapped[str | None] = mapped_column(Text)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class RobotAlert(Base):
    """告警表（hypertable）。主键真实存在，与 init.sql:75-88 逐列一致。

    身份语义：`(time, alert_id)`。
    - 主键必须含分区列 time（TimescaleDB 硬性要求），故顺序为 (time, alert_id)。
    - alert_id 是列级真实主键（BIGSERIAL，库侧自增），唯一标识「同机器人同时刻的
      哪一条」告警：一轮 tick 内 motor_temp / battery_low / joint_stuck 各有独立
      去重窗口，同一 (time, robot_id) 可合法对应多条不同规则告警。
    - 因此**不再需要**映射级 `__mapper_args__` 权宜之计：ORM 身份与 DDL 同源，
      `metadata.create_all()` 不会与 init.sql 漂移。
    - ack_at / ack_by 由订单 19 建字段、订单 17 接 ack/unack 端点；
      escalated 由订单 17 新增（severity=3 超时未确认的升级标记）。
    """

    __tablename__ = "robot_alert"

    time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), primary_key=True)
    alert_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    robot_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    ack: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ack_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    ack_by: Mapped[str | None] = mapped_column(Text)
    # 订单 17：severity=3 且超时未 ack → TRUE。索引 idx_alert_escalation（init.sql:206）。
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # 显式声明 PK 列顺序 (time, alert_id)，与 init.sql 逐字一致。
    # 必要性（已实测）：SQLAlchemy 生成复合 PK 时会把 BIGSERIAL 列排到最前，
    # 只靠 primary_key=True 会编译出 PRIMARY KEY (alert_id, time)，
    # 与 init.sql 的 (time, alert_id) 不同序 —— 契约测试会（且应当）报错。
    # 列声明顺序也与 init.sql 的物理列顺序一致。
    __table_args__ = (PrimaryKeyConstraint("time", "alert_id"),)
