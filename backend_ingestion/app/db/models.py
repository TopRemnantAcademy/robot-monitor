"""SQLAlchemy 2.0 ORM 模型（与 infra/init.sql 严格对齐）。

要点：robot_telemetry 的复合主键为 (time, robot_id) ——
TimescaleDB 要求唯一索引必须包含分区列 time，故顺序不可颠倒。
写路径由 asyncpg 批量完成（见 services/batch_writer.py），此处模型用于读路径与 schema 对齐。
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, Double, Integer, Text, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RobotTelemetry(Base):
    __tablename__ = "robot_telemetry"

    time: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), primary_key=True)
    robot_id: Mapped[str] = mapped_column(Text, primary_key=True)

    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    battery_pct: Mapped[float] = mapped_column(Double, nullable=False)
    battery_temp: Mapped[float | None] = mapped_column(Double)
    max_joint_temp: Mapped[float] = mapped_column(Double, nullable=False)
    joint_temperatures: Mapped[dict] = mapped_column(JSON, nullable=False)
    joint_positions: Mapped[dict] = mapped_column(JSON, nullable=False)
    joint_torques: Mapped[dict] = mapped_column(JSON, nullable=False)
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
    last_seen: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True))


class RobotAlert(Base):
    __tablename__ = "robot_alert"

    time: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), primary_key=True)
    robot_id: Mapped[str] = mapped_column(Text, primary_key=True)
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON)
    ack: Mapped[bool] = mapped_column(Boolean, default=False)
