"""REST/WS response contract (Order 18 — contract-as-code).

[Why this file exists]
FastAPI only writes routes that declared a response_model into /openapi.json.
Previously /robots, /robots/{id}/history, /healthz in app/main.py returned {...}
(bare dicts), so the generated OpenAPI response schema was {} (verified:
{"application/json": {"schema": {}}}) — frontend openapi-typescript would only
get an `unknown`, meaning "contract-as-code" never actually happened.

This module freezes these fields — already returned at runtime — into Pydantic
models, referenced by main.py's response_model=, so /openapi.json finally carries
field-level contracts. Field names align byte-for-byte with existing returns; no
external behavior is changed anywhere.

The WS three-branch (snapshot / telemetry / alert) is outside OpenAPI's scope
(WS is not described by OpenAPI), so it has a separate
docs/contracts/ws-envelope.schema.json + frontend ajv validation, see Order 18 T4.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# --------------------------------------------------------------- joint dict

# Keys are joint names (the 24 standard keys from spec/00 §5.5 + legacy compat),
# values are float. Why dict[str, float] instead of expanding each key: the real
# shape of the backend JSONB column is an arbitrary key->value map; expanding
# would generate 24 required fields, contradicting the "dict length 1~40"
# (spec/01 §3) semantics, and adding/removing joints for a simulator/future
# model would trigger a false-breaking-change alarm.
JointDict = dict[str, float]


# --------------------------------------------------------------- /robots

class RobotTelemetryOut(BaseModel):
    """Latest telemetry for a single robot (matches state_cache._decode()'s return field-by-field).

    Source is the Redis Hash robot:state:{robot_id} restored to typed dict by
    _decode() (backend_ingestion/app/services/state_cache.py).
    Note joint_positions must be in this contract — it is exactly the field
    missing in the STATUS.md §6.2 incident (Drawer's third line was permanently
    empty).

    is_online / last_seen are two NEW optional fields added by ISSUE #77
    (non-breaking: old frontends ignore them). Their source is not the telemetry
    Hash but the receive-side ledger in app/services/liveness.py, backfilled in
    bulk by state_cache.snapshot().
    """

    robot_id: str
    status_code: int = Field(description="0 offline / 1 ok / 2 warning / 3 fault")
    error_code: int = 0
    battery_pct: float
    max_joint_temp: float
    joint_temperatures: JointDict
    joint_positions: JointDict
    joint_torques: JointDict
    ts: int = 0
    # ---- online state (ISSUE #77) ----
    # Send the BACKEND's judgment rather than leaving status_code for the frontend
    # to guess: status_code is the robot's self-reported last frame, frozen after
    # it stops reporting; is_online is the receive-side fact (now - last_seen <=
    # threshold). They are different sources and must be sent separately. The
    # frontend's selectStatusCounts only reads is_online.
    is_online: bool = Field(
        default=False,
        description="Backend judgment: whether last telemetry was received within threshold (ISSUE #77)",
    )
    last_seen: int | None = Field(
        default=None,
        description="epoch seconds of the last telemetry received for this robot; null if never reported",
    )


class RobotListResponse(BaseModel):
    """`GET /robots` — latest status of 100 robots (fallback/first-screen outside WS)."""

    robots: list[RobotTelemetryOut]


# --------------------------------------------------------------- /robots/{id}/history

class HistoryPointOut(BaseModel):
    """A single time bucket from /history (an element of history()'s points[] in main.py)."""

    time: str = Field(description="ISO8601 bucket start time")
    status_code: int
    battery_pct: float
    max_joint_temp: float
    joint_temperatures: JointDict
    joint_torques: JointDict
    joint_positions: JointDict


class RobotHistoryResponse(BaseModel):
    """`GET /robots/{robot_id}/history` (spec/00 §5.3)."""

    robot_id: str
    resolution: str
    points: list[HistoryPointOut]


# --------------------------------------------------------------- /supported-robots

class SupportedRobotOut(BaseModel):
    """A single "platform-supported and actually-registered" model + its registered fleet composition.

    Field sources: model_type / total / online / firmware_versions / stations come
    from the robot_metadata aggregation query; display_name / form_factor / dof /
    vendor / description / generic come from app.catalog.lookup()'s display
    metadata (unregistered models fall back to a placeholder).
    """

    model_type: str = Field(description="model identifier (distinct value of robot_metadata.model_type)")
    display_name: str = Field(description="display name (catalog metadata; falls back to model_type if unregistered)")
    form_factor: str = Field(description="form factor (e.g. bipedal humanoid / -)")
    dof: int = Field(description="degrees of freedom (joint count); 0 for unknown models")
    vendor: str = Field(description="vendor / source")
    description: str = Field(description="model description")
    generic: bool = Field(
        default=False,
        description="true = catalog metadata unregistered, placeholder returned by lookup() (no fabricated display info)",
    )
    total: int = Field(description="total registered robots of this model")
    online: int = Field(description="how many are currently online (is_online)")
    firmware_versions: list[str] = Field(
        default_factory=list, description="distinct firmware versions in use"
    )
    stations: int = Field(default=0, description="distinct count of assigned stations")


class SupportedRobotsResponse(BaseModel):
    """`GET /supported-robots` — catalog of platform-supported and registered models + fleet composition.

    The source of truth for "which models the platform supports" is the distinct
    robot_metadata.model_type (DB-driven); readable from viewer role up (this is a
    public description of platform capabilities, with no write operations).
    """

    models: list[SupportedRobotOut]
    total_robots: int = Field(description="total registered robots across all models")


# --------------------------------------------------------------- /healthz

class HealthResponse(BaseModel):
    """`GET /healthz` liveness probe.

    status expresses DEPENDENCY reachability, not just process liveness, so HTTP
    is always 200 (see main.py docstring). Fields match main.py healthz()'s
    return byte-for-byte, including the row-loss triplet from ISSUE #19.
    """

    status: str = Field(description="ok (DB/Redis reachable) | degraded (process alive but deps unreachable)")
    db: str = Field(description="up | down")
    written: int
    flushes: int
    retries: int
    rejected: int
    queue_depth: int
    mqtt_received: int
    mqtt_dropped: int
    rows_submitted: int
    rows_written: int
    rows_skipped: int
