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

from sqlalchemy import Boolean, Double, Integer, Text, TIMESTAMP
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
    __tablename__ = "robot_alert"

    # ⚠️ robot_alert 是 hypertable，init.sql **没有声明任何主键** ——
    #   它靠 create_hypertable('robot_alert', 'time') 分区，而非靠 PK。
    #
    # 两种错法（均已实测排除）：
    #   ① 在列上写 primary_key=True → metadata.create_all() 会凭空造出 init.sql
    #      不存在的复合主键，语义变成「同一机器人同一毫秒只能有一条告警」，
    #      与 dedup_window_s 去重窗口内可存在多条不同规则告警相冲突。
    #   ② 完全不标主键 → SQLAlchemy 直接抛
    #      ArgumentError: could not assemble any primary key columns（实测）。
    #
    # 正解：列上只标 index=True 表达「按此组合查询」，主键通过 **映射级**
    #   __mapper_args__ 声明。实测：configure_mappers() 通过，且
    #   CreateTable(...).compile(postgresql) 产出的 DDL **不含 PRIMARY KEY**，
    #   与 init.sql 完全一致 —— ORM 拿到身份标识，DDL 不产生多余约束。
    time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, index=True)
    robot_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    ack: Mapped[bool] = mapped_column(Boolean, default=False)

    __mapper_args__ = {"primary_key": [time, robot_id]}
