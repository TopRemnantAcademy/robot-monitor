"""Supported robot-model catalog (DB-driven + extensible static metadata).

Design (user decision 2026-11-09: "DB-based HUMANOID-24DOF + extended catalog"):
- The **source of truth** for "which models the platform supports" is the
  distinct values of `robot_metadata.model_type` (DB-driven, zero hallucination).
- The static dict in this module only supplements **display metadata**
  (display_name / description / DOF / form factor / vendor), keyed by model_type.
  Adding a new model only needs one more line here; query logic stays unchanged.
- If the DB has a model_type not covered by this dict, `lookup()` falls back to a
  "generic placeholder" (generic=True) and **never fabricates** display info.

Metadata strictly corresponds to code facts (avoid hallucination):
- HUMANOID-24DOF: frontend_dashboard/src/types/robot.ts's JOINT_NAMES is exactly
  24 standard joints => 24 DOF; form = bipedal humanoid (humanoid, robot_id
  prefix `humanoid-`); vendor = self-developed on this platform (no third-party
  source, so no fabricated external vendor name).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RobotModelMeta:
    model_type: str
    display_name: str
    form_factor: str
    dof: int
    vendor: str
    description: str
    generic: bool = False


# Only include models actually present in the DB (init.sql:48 pre-seeds all 100
# units as HUMANOID-24DOF).
ROBOT_CATALOG: dict[str, RobotModelMeta] = {
    "HUMANOID-24DOF": RobotModelMeta(
        model_type="HUMANOID-24DOF",
        display_name="Humanoid 24-DOF",
        form_factor="bipedal humanoid",
        dof=24,
        vendor="RobotMonitor Platform",
        description=(
            "The platform's main model: a 24-DOF highly anthropomorphic bipedal "
            "robot covering shoulder/elbow/wrist/hip/knee/ankle joint telemetry, "
            "supporting real-time temperature, torque, and attitude-angle monitoring "
            "and alerting."
        ),
    ),
}


def lookup(model_type: str | None) -> RobotModelMeta:
    """Get display metadata by model_type; if unregistered, fall back to a
    placeholder (generic=True), no fabrication."""
    if model_type and model_type in ROBOT_CATALOG:
        return ROBOT_CATALOG[model_type]
    return RobotModelMeta(
        model_type=model_type or "UNKNOWN",
        display_name=model_type or "Unknown model",
        form_factor="—",
        dof=0,
        vendor="—",
        description="Model present in the DB but not yet registered in the catalog metadata; contact an admin to add it.",
        generic=True,
    )
