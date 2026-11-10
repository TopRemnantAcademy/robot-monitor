"""SQLAlchemy 2.0 ORM models (strictly aligned with infra/init.sql).

Key points:
- robot_telemetry's composite primary key is (time, robot_id) --
  TimescaleDB requires the unique index to include the partitioning column time,
  so the order must not be reversed.
- The three joint columns and detail must be **JSONB** (init.sql uses JSONB).
  Using sqlalchemy.JSON compiles to `JSON` (not JSONB) -> ORM-side DDL diverges
  from the real in-DB type, causing metadata drift and losing JSONB's index/
  operator capabilities.
  See models.py:RobotTelemetry.joint_temperatures's JSONB annotation.

The write path is done by asyncpg in bulk (see services/batch_writer.py); the
models here are for the read path and schema alignment.
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
    battery_pct: Mapped[float | None] = mapped_column(Double)
    battery_temp: Mapped[float | None] = mapped_column(Double)
    max_joint_temp: Mapped[float | None] = mapped_column(Double)
    joint_temperatures: Mapped[dict | None] = mapped_column(JSONB)
    joint_positions: Mapped[dict | None] = mapped_column(JSONB)
    joint_torques: Mapped[dict | None] = mapped_column(JSONB)
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
    """Alert table (hypertable). The primary key really exists, column-by-column
    consistent with init.sql:75-88.

    Identity semantics: `(time, alert_id)`.
    - The primary key must include the partitioning column time (hard TimescaleDB
      requirement), so the order is (time, alert_id).
    - alert_id is a real column-level primary key (BIGSERIAL, DB-side auto-
      increment), uniquely identifying "which one" of multiple alerts at the same
      robot and same moment: within one tick, motor_temp / battery_low /
      joint_stuck each have an independent dedup window, so the same (time,
      robot_id) can legitimately correspond to multiple different-rule alerts.
    - Therefore the mapping-level `__mapper_args__` stopgap is **no longer
      needed**: ORM identity and DDL are co-sourced, and `metadata.create_all()`
      will not drift from init.sql.
    - ack_at / ack_by were created by order 19 and wired to ack/unack endpoints by
      order 17; escalated was added by order 17 (severity=3 unacked-timeout
      escalation flag); suppressed / suppressed_by were added by order 35
      (suppression persisted, so the REST contract can query suppressed alerts).
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
    # Order 17: severity=3 and unacked past timeout -> TRUE. Index
    # idx_alert_escalation (init.sql:206).
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Order 35: suppression flag (TRUE = judged suppressed: no WS push, no
    # notification, but row retained and queryable).
    # Column order, type, default must be column-by-column consistent with
    # init.sql's robot_alert -- tests/test_schema_contract.py's
    # `init["cols"] == orm["cols"]` will catch drift on the spot.
    suppressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The root-cause rule that suppressed it (offline / motor_temp / ...), paired
    # with suppressed (must be non-null when TRUE).
    suppressed_by: Mapped[str | None] = mapped_column(Text)

    # Explicitly declare PK column order (time, alert_id), verbatim consistent
    # with init.sql.
    # Necessity (measured): when SQLAlchemy generates a composite PK it puts the
    # BIGSERIAL column first; relying only on primary_key=True would compile
    # PRIMARY KEY (alert_id, time), differing in order from init.sql's
    # (time, alert_id) -- the contract test would (and should) error.
    # The column declaration order also matches init.sql's physical column order.
    __table_args__ = (PrimaryKeyConstraint("time", "alert_id"),)
