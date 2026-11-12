"""VLA policy-action batch writer (R-VLA-EVAL Phase 1).

Reuses the EXACT same degradation chain as ``batch_writer.BatchWriter``
(COPY -> staging upsert / executemany / binary-search bad-row isolation), which is
battle-tested for the telemetry stream at 1000 rows/s. The only differences are
the target hypertable, its column set, and the row shape -- so the action stream
inherits all the fault-tolerance properties of the telemetry writer for free,
with zero changes to the proven path.

Why a subclass and not a second copy
------------------------------------
The degradation logic lives in ``BatchWriter`` (COPY/executemany/isolate); this
module only overrides the table-specific bits (config + ``_to_row``). The
telemetry writer keeps ``BatchWriter()`` unchanged, so existing behavior and its
test suite are untouched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.schemas.telemetry import PolicyAction
from app.services.batch_writer import BatchWriter, WriterConfig

# policy_actions column set (mirrors infra/init.sql §2.1 / migration 002).
_POLICY_ACTION_COLUMNS: tuple[str, ...] = (
    "time", "robot_id", "policy_version", "trial_id", "action", "goal",
)

POLICY_ACTION_CONFIG = WriterConfig(
    staging_table="policy_actions_stg",
    columns=_POLICY_ACTION_COLUMNS,
    insert_sql="""
INSERT INTO policy_actions (time, robot_id, policy_version, trial_id, action, goal)
VALUES ($1,$2,$3,$4,$5::jsonb,$6)
ON CONFLICT (time, robot_id) DO NOTHING
""",
    staging_create="CREATE TEMP TABLE IF NOT EXISTS policy_actions_stg (LIKE policy_actions INCLUDING DEFAULTS)",
    staging_truncate="TRUNCATE policy_actions_stg",
    staging_upsert=f"""
INSERT INTO policy_actions ({", ".join(_POLICY_ACTION_COLUMNS)})
SELECT {", ".join(_POLICY_ACTION_COLUMNS)} FROM policy_actions_stg
ON CONFLICT (time, robot_id) DO NOTHING
""",
)


class PolicyActionBatchWriter(BatchWriter):
    """Batch writer for the VLA ``policy_actions`` stream.

    Same dual-trigger flush (count=500 / 1s), same L1->L2->L3 degradation, same
    offline-buffer hook -- only the destination table differs.
    """

    def __init__(self) -> None:
        super().__init__(POLICY_ACTION_CONFIG)

    @staticmethod
    def _to_row(t: PolicyAction) -> tuple:
        # Prefer the client timestamp (10Hz -> adjacent rows for the same robot
        # differ by 100ms). If we used flush-time now() uniformly, multiple
        # records for the same robot in one batch could collide on the (time,
        # robot_id) PK and be silently dropped by ON CONFLICT DO NOTHING.
        if t.ts:
            ts = datetime.fromtimestamp(t.ts / 1000.0, tz=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)
        # action is a JSONB column: pass JSON text (passing a list raises
        # TypeError on the COPY path), the ::jsonb cast handles the L2 path.
        return (
            ts,
            t.robot_id,
            t.policy_version,
            t.trial_id,
            json.dumps(t.action),
            t.goal,
        )
