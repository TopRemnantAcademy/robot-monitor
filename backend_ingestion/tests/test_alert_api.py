"""订单 17 T1 —— 告警闭环 REST 端点的 HTTP 层验收测试。

与 `verify_alert_closure.py` 的分工：
- 后者直接打 SQL 层（repository），验证 SQL 语义（幂等、分页、升级标记）；
- 本文件走**真实 FastAPI 路由**（TestClient，同步 ASGI），验证
  鉴权边界（401/403）、请求装配、响应 schema —— 这些只有经过 HTTP 栈才暴露。

依赖真实 PostgreSQL（`robot_alert` 表已由 init.sql 建好）+ Redis 可选。
未提供 `TEST_DATABASE_URL` 时，整文件 skip（不伪装成通过）。

跑法：
    export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/robot_monitor
    export JWT_SECRET=test-secret
    cd backend_ingestion && python -m pytest tests/test_alert_api.py -v
"""

from __future__ import annotations

import os
import time

import pytest

DSN = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="需要 TEST_DATABASE_URL（真实 PostgreSQL）")

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
    """注册（默认 viewer）→ 登录，返回 access_token。"""
    r = client.post("/auth/register", json={"email": email, "password": "pw12345678"})
    assert r.status_code in (201, 400), r.text  # 400 = 已存在（重跑）
    r = client.post(
        "/auth/jwt/login",
        data={"username": email, "password": "pw12345678"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _seed(alert_id_out: list) -> None:
    """用同步 SQLAlchemy/asyncpg 插一条告警供测试。"""
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
    """注册 → 提权为 admin（改库，见 init.sql 的提权说明）→ 重新登录拿带 role 的 token。"""
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
    """无 token → 401（不是 403）：没凭据与凭据不足是两种语义。"""
    assert client.get("/alerts").status_code == 401
    assert client.post("/alerts/1/ack").status_code == 401


def test_viewer_can_read_but_not_ack(client):
    """viewer 可读 /alerts（与 /robots 同级），但 ack 得 403。"""
    tok = _register_and_login(client, f"viewer17-{int(time.time())}@example.com")
    h = {"Authorization": f"Bearer {tok}"}
    r = client.get("/alerts?ack=false&limit=50&offset=0", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["offset"] == 0 and r.json()["limit"] == 50
    assert client.post("/alerts/1/ack", headers=h).status_code == 403


def test_ack_writes_ack_by_from_jwt(client, admin_token):
    """admin ack 后，ack_by 来自 JWT（不是请求体）。"""
    ids: list = []
    _seed(ids)
    aid = ids[0]
    h = {"Authorization": f"Bearer {admin_token}"}
    r = client.post(f"/alerts/{aid}/ack", headers=h, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ack"] is True and body["changed"] is True
    assert body["ack_by"] is not None
    # 幂等：再 ack 一次，changed=False
    r2 = client.post(f"/alerts/{aid}/ack", headers=h, json={})
    assert r2.status_code == 200 and r2.json()["changed"] is False
    # unack
    r3 = client.post(f"/alerts/{aid}/unack", headers=h, json={})
    assert r3.status_code == 200 and r3.json()["ack"] is False


def test_cannot_forge_ack_by(client, admin_token):
    """派工单禁止项 2：请求体传 ack_by 必须被拒（extra=forbid → 422）。"""
    h = {"Authorization": f"Bearer {admin_token}"}
    r = client.post("/alerts/1/ack", headers=h, json={"ack_by": "attacker"})
    assert r.status_code == 422, r.text


def test_ack_missing_alert_404(client, admin_token):
    h = {"Authorization": f"Bearer {admin_token}"}
    assert client.post("/alerts/999999999/ack", headers=h, json={}).status_code == 404


def test_suppressed_filter_and_field(client, admin_token):
    """订单 35：`GET /alerts?suppressed=…` 三态过滤 + 响应带 suppressed 字段。

    需要真实 PostgreSQL —— 本文件顶部已 skipif（未提供 TEST_DATABASE_URL 时不伪装通过）。
    断言的语义：
      - 省略 suppressed → 返回全集（契约刻意保持中立，默认值一变就会静默改变既有调用方行为）；
      - suppressed=false → 排除被抑制行（前端默认视图的口径）；
      - suppressed=true  → 只返回被抑制行，且 suppressed_by 指向根因规则。
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
                tag, f"{tag} 被 offline 压住",
            )
            await conn.execute(
                """INSERT INTO robot_alert
                       (time, robot_id, rule_id, severity, message, detail, suppressed, suppressed_by)
                   VALUES (NOW(), 'humanoid-099', $1, 2, $2, '{}'::jsonb, FALSE, NULL)""",
                tag, f"{tag} 正常告警",
            )
        finally:
            await conn.close()

    asyncio.run(_seed())
    h = {"Authorization": f"Bearer {admin_token}"}

    # 按本次 RUN_TAG 唯一化 rule_id 并客户端过滤：同一 robot_id 重跑会累积旧行，
    # 直接断言 len(items)==2 会变成「跑第二次就红」的假失败（本机实测踩到过）。
    def _mine(items: list) -> list:
        return [i for i in items if i["rule_id"] == tag]

    r = client.get(f"/alerts?robot_id=humanoid-099&limit=50", headers=h)
    assert r.status_code == 200, r.text
    items = _mine(r.json()["items"])
    assert len(items) == 2, f"省略 suppressed 时应返回全集，实际 {items}"
    # 新字段必须出现在响应里（不是只在 DB 里）
    suppressed_row = next(i for i in items if i["suppressed"] is True)
    normal_row = next(i for i in items if i["suppressed"] is False)
    assert suppressed_row["suppressed_by"] == "offline"
    assert normal_row["suppressed_by"] is None

    r2 = client.get(f"/alerts?robot_id=humanoid-099&suppressed=false&limit=50", headers=h)
    assert r2.status_code == 200, r2.text
    assert [i["suppressed"] for i in _mine(r2.json()["items"])] == [False]
    # total 必须随筛选条件变化（不只是 items 被截）—— 用同条件 count 交叉验证，
    # 避免与库里其他 tag 的行相互干扰。
    total_all = client.get("/alerts?robot_id=humanoid-099&limit=1", headers=h).json()["total"]
    total_ok = client.get(
        "/alerts?robot_id=humanoid-099&suppressed=false&limit=1", headers=h
    ).json()["total"]
    assert total_ok < total_all, "suppressed=false 的 total 必须小于全集 total"

    r3 = client.get(f"/alerts?robot_id=humanoid-099&suppressed=true&limit=50", headers=h)
    assert r3.status_code == 200, r3.text
    mine_sup = _mine(r3.json()["items"])
    assert [i["suppressed"] for i in mine_sup] == [True]
    assert mine_sup[0]["suppressed_by"] == "offline"


def test_pagination_offset_effective(client, admin_token):
    """GET /alerts?ack=false&limit=1&offset=N 的 offset 真正生效。"""
    ids: list = []
    for _ in range(3):
        ids.append([])
        _seed(ids[-1])
    h = {"Authorization": f"Bearer {admin_token}"}
    p0 = client.get("/alerts?ack=false&limit=1&offset=0", headers=h).json()
    p1 = client.get("/alerts?ack=false&limit=1&offset=1", headers=h).json()
    assert p0["items"][0]["alert_id"] != p1["items"][0]["alert_id"]
    assert p0["total"] == p1["total"] >= 3
    # 校验 limit 越界
    assert client.get("/alerts?limit=0", headers=h).status_code == 422
    assert client.get("/alerts?limit=999", headers=h).status_code == 422
