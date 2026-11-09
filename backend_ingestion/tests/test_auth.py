"""认证与 RBAC 的行为契约测试（订单 15）。

本文件锁定的是**验收标准**本身，不是实现细节 —— 每条断言都能对应到
`orders/15-dispatch.md` §2 的一行，改实现只要语义不变就不该红。

跑法（从仓库根）：
    python -m pytest backend_ingestion/tests/test_auth.py -v

关于 `import backend_app`：本仓有 `backend_ingestion/app` 与 `alert_engine/app`
两个同名顶层包，从仓库根跑 pytest 时后导入的会失败。`conftest.py` 把前者
以 `backend_app` 别名加载，故这里统一走别名（详见该文件头注释）。

为什么不用真实数据库：鉴权判定发生在**验签 + claims 解析**这一步，与用户表内容无关
（WS 路径完全不查库；REST 路径查库也只是确认用户还在）。真实库的端到端验收已在
PR 里用 PostgreSQL 15.19 实跑取证。这样本文件在**任何装了依赖的机器**上都能跑，
不要求 PG/Redis/MQTT。
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


# ------------------------------------------------------------------ 纯逻辑

class TestRoleInclusion:
    """两级 RBAC 的包含关系。这是权限模型的地基，值得单独钉住。"""

    def test_admin_satisfies_both(self):
        assert satisfies("admin", Role.ADMIN) is True
        assert satisfies("admin", Role.VIEWER) is True

    def test_viewer_only_reads(self):
        assert satisfies("viewer", Role.VIEWER) is True
        assert satisfies("viewer", Role.ADMIN) is False

    def test_unknown_role_denied(self):
        # 库里的 role 被手工改成垃圾值时**必须**拒绝，而不是当作 viewer 放行：
        # 放行等于让 CHECK 约束外的值意外获得读权限，且没有任何迹象。
        assert satisfies("superuser", Role.VIEWER) is False
        assert satisfies("", Role.VIEWER) is False

    def test_default_role_is_viewer_not_admin(self):
        # 自助注册默认只读。改成 admin 会让 /auth/register 变成自助提权后门。
        assert DEFAULT_ROLE is Role.VIEWER
        assert Role.ADMIN in ALL_ROLES


class TestJwtClaims:
    """角色必须进 claims（T3），否则前端拿不到、订单 17 也没法判权限。"""

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
    """WS 只验签、不查库（长连接不能每帧查库）。"""

    def test_missing_token_rejected(self):
        assert asyncio.run(authenticate_ws_token(None)) is None
        assert asyncio.run(authenticate_ws_token("")) is None

    def test_garbage_token_rejected(self):
        assert asyncio.run(authenticate_ws_token("not-a-jwt")) is None

    def test_token_signed_with_other_key_rejected(self):
        """换成别人的密钥签的 token 必须验不过 —— 这是"验签"存在的全部意义。"""
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


# ------------------------------------------------------------------ HTTP 层

@pytest.fixture
def client():
    """TestClient 不跑 lifespan（不连 MQTT/Redis/PG），但路由与依赖解析完全真实。"""
    return TestClient(app, raise_server_exceptions=False)


class TestWhitelist:
    """免鉴权白名单必须**恰好**是 /healthz 与 /metrics（订单 15 禁止项 4、8）。"""

    def test_healthz_no_auth(self, client):
        assert client.get("/healthz").status_code == 200

    def test_metrics_no_auth(self, client):
        """⚠️ 给 /metrics 加鉴权 = Prometheus 抓不到 = 可观测性静默失效。"""
        r = client.get("/metrics")
        assert r.status_code == 200
        # prometheus-client 0.26.0 默认 exposition 为 version=1.0.0（0.21.1 为 0.0.4，
        # 并带 "; charset=utf-8" 后缀）。断言只校验稳定的 "text/plain" MIME 族——跨 client
        # 版本不脆，且足以证明这是 Prometheus 文本暴露格式而非 HTML/JSON 错误页。
        assert r.headers["content-type"].startswith("text/plain")


class TestRestAuth:
    """未带/带错 token 一律 401，且不得是 403（验收标准明确区分）。"""

    def test_robots_without_token_is_401(self, client):
        assert client.get("/robots").status_code == 401, "必须是 401，不是 403/500"

    def test_robots_with_bad_token_is_401(self, client):
        r = client.get("/robots", headers={"Authorization": "Bearer garbage"})
        assert r.status_code == 401

    def test_history_without_token_is_401(self, client):
        assert client.get("/robots/humanoid-001/history").status_code == 401

    def test_token_in_query_is_not_accepted_for_rest(self, client):
        """禁止项 10：REST 只认 Authorization 头。

        query 传 token 会被写进 nginx/网关访问日志 —— 那是 WS 才不得不开的例外。
        """
        assert client.get("/robots?token=whatever").status_code == 401


class TestRegisterSchema:
    """注册请求不得能指定角色（自助提权后门）。"""

    def test_role_field_rejected(self, client):
        r = client.post(
            "/auth/register",
            json={"email": "a@b.example.com", "password": "Passw0rd!x", "role": "admin"},
        )
        assert r.status_code == 422, "注册夹带 role 必须 422，不能被静默忽略"
