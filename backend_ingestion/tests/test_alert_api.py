"""Order 17 T1 -- HTTP-layer acceptance tests for the alert-closure REST endpoints.

Division of labor with `verify_alert_closure.py`:
- The latter hits the SQL layer (repository) directly to verify SQL semantics
  (idempotency, pagination, escalation markers);
- This file exercises the **real FastAPI routes** (TestClient, synchronous ASGI)
  to verify auth boundaries (401/403), request assembly, and response schema --
  these are only exposed once traffic has passed through the HTTP stack.

Depends on a real PostgreSQL (`robot_alert` table is created by init.sql) + optional Redis.
When `TEST_DATABASE_URL` is not provided, the whole file is skipped (not faked as passing).

Run:
    export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/robot_monitor
    export JWT_SECRET=test-secret
    cd backend_ingestion && python -m pytest tests/test_alert_api.py -v
"""

from __future__ import annotations

import os
import time

import pytest

DSN = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="requires TEST_DATABASE_URL (real PostgreSQL)")

if DSN:
    os.environ["DATABASE_URL"] = DSN

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

RUN_TAG = f"apitest17-{int(time.time())}"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _register_and_login(client: TestClient, email: str) -> str:
    """Register (default role: viewer) -> log in, return access_token."""
    r = client.post("/auth/register", json={"email": email, "password": "pw12345678"})
    assert r.status_code in (201, 400), r.text  # 400 = already exists (re-run)
    r = client.post(
        "/auth/jwt/login",
        data={"username": email, "password": "pw12345678"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _seed(alert_id_out: list) -> None:
    """Insert one alert for testing using synchronous asyncpg."""
    import asyncio

    import asyncpg

    async def _run():
        conn = await asyncpg.connect(DSN)
        try:
            aid = await conn.fetchval(
                """INSERT INTO robot_alert (time, robot_id, rule_id, severity, message, detail)
                   VALUES (NOW(), 'humanoid-001', $1, 3, $2, '{}'::jsonb)
                   RETURNING alert_id""",
                RUN_TAG, f"{RUN_TAG} seed",
            )
            alert_id_out.append(aid)
        finally:
            await conn.close()

    asyncio.run(_run())


@pytest.fixture(scope="module")
def admin_token(client):
    """Register -> promote to admin (mutates DB; see init.sql promotion note) -> log in again to obtain a role-bearing token."""
    import asyncio

    import asyncpg

    email = f"admin17-{int(time.time())}@example.com"
    _register_and_login(client, email)

    async def _promote():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "UPDATE robot_user SET role='admin' WHERE email=$1", email
            )
        finally:
            await conn.close()

    asyncio.run(_promote())
    return _register_and_login(client, email)


def test_requires_auth_401(client):
    """No token -> 401 (not 403): missing credentials vs insufficient privileges are distinct semantics."""
    assert client.get("/alerts").status_code == 401
    assert client.post("/alerts/1/ack").status_code == 401


def test_viewer_can_read_but_not_ack(client):
    """viewer may read /alerts (same level as /robots) but gets 403 on ack."""
    tok = _register_and_login(client, f"viewer17-{int(time.time())}@example.com")
    h = {"Authorization": f"Bearer {tok}"}
    r = client.get("/alerts?ack=false&limit=50&offset=0", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["offset"] == 0 and r.json()["limit"] == 50
    assert client.post("/alerts/1/ack", headers=h).status_code == 403


def test_ack_writes_ack_by_from_jwt(client, admin_token):
    """After admin ack, ack_by comes from the JWT (not the request body)."""
    ids: list = []
    _seed(ids)
    aid = ids[0]
    h = {"Authorization": f"Bearer {admin_token}"}
    r = client.post(f"/alerts/{aid}/ack", headers=h, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ack"] is True and body["changed"] is True
    assert body["ack_by"] is not None
    # Idempotent: ack again -> changed=False
    r2 = client.post(f"/alerts/{aid}/ack", headers=h, json={})
    assert r2.status_code == 200 and r2.json()["changed"] is False
    # unack
    r3 = client.post(f"/alerts/{aid}/unack", headers=h, json={})
    assert r3.status_code == 200 and r3.json()["ack"] is False


def test_cannot_forge_ack_by(client, admin_token):
    """Work-order forbidden item #2: passing ack_by in the body must be rejected (extra=forbid -> 422)."""
    h = {"Authorization": f"Bearer {admin_token}"}
    r = client.post("/alerts/1/ack", headers=h, json={"ack_by": "attacker"})
    assert r.status_code == 422, r.text


def test_ack_missing_alert_404(client, admin_token):
    h = {"Authorization": f"Bearer {admin_token}"}
    assert client.post("/alerts/999999999/ack", headers=h, json={}).status_code == 404


def test_suppressed_filter_and_field(client, admin_token):
    """Order 35: `GET /alerts?suppressed=...` three-state filter + response carries suppressed field.

    Requires a real PostgreSQL -- this file already skipifs at the top (does not fake-pass
    when TEST_DATABASE_URL is absent).
    Asserted semantics:
      - omit suppressed -> return the full set (the contract deliberately stays neutral;
        changing the default would silently alter existing caller behavior);
      - suppressed=false -> exclude suppressed rows (the frontend default-view criteria);
      - suppressed=true  -> return only suppressed rows, with suppressed_by pointing to the
        root-cause rule.
    """
    tag = f"{RUN_TAG}-sup"
    import asyncio

    import asyncpg

    async def _seed() -> None:
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                """INSERT INTO robot_alert
                       (time, robot_id, rule_id, severity, message, detail, suppressed, suppressed_by)
                   VALUES (NOW(), 'humanoid-099', $1, 3, $2, '{}'::jsonb, TRUE, 'offline')""",
                tag, f"{tag} suppressed by offline",
            )
            await conn.execute(
                """INSERT INTO robot_alert
                       (time, robot_id, rule_id, severity, message, detail, suppressed, suppressed_by)
                   VALUES (NOW(), 'humanoid-099', $1, 2, $2, '{}'::jsonb, FALSE, NULL)""",
                tag, f"{tag} normal alert",
            )
        finally:
            await conn.close()

    asyncio.run(_seed())
    h = {"Authorization": f"Bearer {admin_token}"}

    # Uniquify rule_id per RUN_TAG and filter client-side: re-running the same robot_id
    # accumulates stale rows, so asserting len(items)==2 outright would turn into a
    # "fails on the second run" false negative (hit this locally before).
    def _mine(items: list) -> list:
        return [i for i in items if i["rule_id"] == tag]

    r = client.get(f"/alerts?robot_id=humanoid-099&limit=50", headers=h)
    assert r.status_code == 200, r.text
    items = _mine(r.json()["items"])
    assert len(items) == 2, f"Omitting suppressed should return the full set, got {items}"
    # The new field must appear in the response (not only in the DB)
    suppressed_row = next(i for i in items if i["suppressed"] is True)
    normal_row = next(i for i in items if i["suppressed"] is False)
    assert suppressed_row["suppressed_by"] == "offline"
    assert normal_row["suppressed_by"] is None

    r2 = client.get(f"/alerts?robot_id=humanoid-099&suppressed=false&limit=50", headers=h)
    assert r2.status_code == 200, r2.text
    assert [i["suppressed"] for i in _mine(r2.json()["items"])] == [False]
    # total must change with the filter (not just items being truncated) -- cross-check
    # with a same-condition count to avoid interference from other tags' rows in the DB.
    total_all = client.get("/alerts?robot_id=humanoid-099&limit=1", headers=h).json()["total"]
    total_ok = client.get(
        "/alerts?robot_id=humanoid-099&suppressed=false&limit=1", headers=h
    ).json()["total"]
    assert total_ok < total_all, "total for suppressed=false must be smaller than the full-set total"

    r3 = client.get(f"/alerts?robot_id=humanoid-099&suppressed=true&limit=50", headers=h)
    assert r3.status_code == 200, r3.text
    mine_sup = _mine(r3.json()["items"])
    assert [i["suppressed"] for i in mine_sup] == [True]
    assert mine_sup[0]["suppressed_by"] == "offline"


def test_pagination_offset_effective(client, admin_token):
    """GET /alerts?ack=false&limit=1&offset=N -- offset really takes effect."""
    ids: list = []
    for _ in range(3):
        ids.append([])
        _seed(ids[-1])
    h = {"Authorization": f"Bearer {admin_token}"}
    p0 = client.get("/alerts?ack=false&limit=1&offset=0", headers=h).json()
    p1 = client.get("/alerts?ack=false&limit=1&offset=1", headers=h).json()
    assert p0["items"][0]["alert_id"] != p1["items"][0]["alert_id"]
    assert p0["total"] == p1["total"] >= 3
    # validate limit out-of-range
    assert client.get("/alerts?limit=0", headers=h).status_code == 422
    assert client.get("/alerts?limit=999", headers=h).status_code == 422
