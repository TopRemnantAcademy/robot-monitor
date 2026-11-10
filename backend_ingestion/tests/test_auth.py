"""Authentication & RBAC behavior-contract tests (Order 15).

This file locks down the **acceptance criteria** themselves, not implementation
details -- every assertion maps to a line in `orders/15-dispatch.md` section 2, so
changing the implementation should not turn red as long as the semantics are unchanged.

Run from the repo root:
    python -m pytest backend_ingestion/tests/test_auth.py -v

About `import backend_app`: this repo has two same-named top-level packages
`backend_ingestion/app` and `alert_engine/app`; running pytest from the repo root
would fail for the one imported later. `conftest.py` loads the former under the
`backend_app` alias, so we consistently use that alias here (see that file's header
comment).

Why no real database: the auth decision happens at the **signature verification +
claims parsing** step and is independent of the user-table content (the WS path does
not hit the DB at all; the REST path only checks the user still exists). End-to-end
verification against a real DB was already done in the PR with PostgreSQL 15.19.
So this file runs on **any machine with the dependencies installed**, without
requiring PG/Redis/MQTT.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.auth.backend import RoleAwareJWTStrategy
from app.auth.deps import authenticate_ws_token, satisfies
from app.auth.roles import ALL_ROLES, DEFAULT_ROLE, Role
from app.auth.users import RobotUser
from app.main import app

from fastapi.testclient import TestClient


# ------------------------------------------------------------------ pure logic

class TestRoleInclusion:
    """The two-tier RBAC inclusion relationship. This is the foundation of the
    permission model and worth pinning down on its own."""

    def test_admin_satisfies_both(self):
        assert satisfies("admin", Role.ADMIN) is True
        assert satisfies("admin", Role.VIEWER) is True

    def test_viewer_only_reads(self):
        assert satisfies("viewer", Role.VIEWER) is True
        assert satisfies("viewer", Role.ADMIN) is False

    def test_unknown_role_denied(self):
        # A role hand-edited to garbage in the DB **must** be rejected, not silently
        # treated as viewer: allowing it would let a value outside the CHECK constraint
        # accidentally gain read access, with no signal at all.
        assert satisfies("superuser", Role.VIEWER) is False
        assert satisfies("", Role.VIEWER) is False

    def test_default_role_is_viewer_not_admin(self):
        # Self-service registration defaults to read-only. Making it admin would turn
        # /auth/register into a self-service privilege-escalation backdoor.
        assert DEFAULT_ROLE is Role.VIEWER
        assert Role.ADMIN in ALL_ROLES


class TestJwtClaims:
    """The role must be present in the claims (T3), otherwise the frontend cannot
    read it and Order 17 cannot decide permissions."""

    def test_write_token_carries_sub_and_role(self):
        import jwt as pyjwt

        strategy = RoleAwareJWTStrategy(secret="x" * 32, lifetime_seconds=60)
        user = RobotUser(id=7, email="a@b.example.com", hashed_password="h", role="admin")
        token = asyncio.run(strategy.write_token(user))
        claims = pyjwt.decode(
            token, "x" * 32, audience=["fastapi-users:auth"], algorithms=["HS256"]
        )
        assert claims["sub"] == "7"
        assert claims["role"] == "admin"


class TestWsTokenVerification:
    """WS only verifies the signature, no DB lookup (a long-lived connection cannot
    hit the DB per frame)."""

    def test_missing_token_rejected(self):
        assert asyncio.run(authenticate_ws_token(None)) is None
        assert asyncio.run(authenticate_ws_token("")) is None

    def test_garbage_token_rejected(self):
        assert asyncio.run(authenticate_ws_token("not-a-jwt")) is None

    def test_token_signed_with_other_key_rejected(self):
        """A token signed with someone else's key must fail verification -- that is the
        entire point of signature verification."""
        import jwt as pyjwt

        forged = pyjwt.encode(
            {"sub": "1", "aud": ["fastapi-users:auth"], "role": "admin"},
            "attacker-key",
            algorithm="HS256",
        )
        assert asyncio.run(authenticate_ws_token(forged)) is None

    def test_valid_token_returns_sub(self):
        import jwt as pyjwt

        good = pyjwt.encode(
            {"sub": "42", "aud": ["fastapi-users:auth"], "role": "viewer"},
            os.environ["JWT_SECRET"],
            algorithm="HS256",
        )
        assert asyncio.run(authenticate_ws_token(good)) == "42"


# ------------------------------------------------------------------ HTTP layer

@pytest.fixture
def client():
    """TestClient does not run lifespan (no MQTT/Redis/PG connection), but routes and
    dependency resolution are fully real."""
    return TestClient(app, raise_server_exceptions=False)


class TestWhitelist:
    """The auth-exempt whitelist must be **exactly** /healthz and /metrics
    (Order 15 forbidden items 4, 8)."""

    def test_healthz_no_auth(self, client):
        assert client.get("/healthz").status_code == 200

    def test_metrics_no_auth(self, client):
        """WARNING: adding auth to /metrics = Prometheus can't scrape = observability
        silently fails."""
        r = client.get("/metrics")
        assert r.status_code == 200
        # prometheus-client 0.26.0 defaults exposition to version=1.0.0 (0.21.1 was
        # 0.0.4, with a "; charset=utf-8" suffix). We only assert the stable
        # "text/plain" MIME family -- cross-client-version robust and sufficient to prove
        # this is the Prometheus text exposition format, not an HTML/JSON error page.
        assert r.headers["content-type"].startswith("text/plain")


class TestRestAuth:
    """Missing or wrong token -> always 401, never 403 (acceptance criteria make the
    distinction explicit)."""

    def test_robots_without_token_is_401(self, client):
        assert client.get("/robots").status_code == 401, "Must be 401, not 403/500"

    def test_robots_with_bad_token_is_401(self, client):
        r = client.get("/robots", headers={"Authorization": "Bearer garbage"})
        assert r.status_code == 401

    def test_history_without_token_is_401(self, client):
        assert client.get("/robots/humanoid-001/history").status_code == 401

    def test_token_in_query_is_not_accepted_for_rest(self, client):
        """Forbidden item 10: REST only accepts the Authorization header.

        Passing the token in the query string would land it in nginx/gateway access
        logs -- that exception is only unavoidable for WS.
        """
        assert client.get("/robots?token=whatever").status_code == 401


class TestRegisterSchema:
    """The registration request must not be able to specify a role
    (self-service privilege-escalation backdoor)."""

    def test_role_field_rejected(self, client):
        r = client.post(
            "/auth/register",
            json={"email": "a@b.example.com", "password": "Passw0rd!x", "role": "admin"},
        )
        assert r.status_code == 422, "Registration carrying role must be 422, not silently ignored"
