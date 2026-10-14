"""R-17 —— env 驱动 CORS + 可选限流的回归测试。

锁定的是 R-17 §5 验收标准（V1~V3、V5），不是实现细节：改实现只要语义不变就不该红。

跑法（**必须单独跑**，理由见 pytest.ini 与 conftest.py 头注释）：
    python -m pytest backend_ingestion/tests/test_security_middleware.py -v

不需要 PG / Redis / MQTT / MQTT broker：
- CORS / 限流都是 ASGI 中间件，用 TestClient（不跑 lifespan，不连依赖）即可覆盖。
- 被测 app 用 `app.main.app`（**真实**装配顺序：中间件在 include_router 之前挂载），
  而不是自己拼一个 FastAPI —— 后者证明不了"生产里真的挂上了"。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

from app.main import app as real_app
from app.security_middleware import (
    EXEMPT_PATHS,
    INTERNAL_PATH_PREFIX,
    RateLimitMiddleware,
    install_cors,
    install_rate_limit,
)


def _app_with_routes() -> FastAPI:
    """最小 FastAPI：一个普通路由 + 两个关键路径（/healthz、/metrics）。

    关键路径在测试里用假实现（不连 DB/Redis）—— 本文件考的是**中间件是否放行**，
    不是这两个端点的业务逻辑（那由 test_observability.py 覆盖）。
    """
    a = FastAPI()

    @a.get("/robots")
    async def _robots():  # pragma: no cover - 只被路由表用到
        return {"robots": []}

    @a.get("/healthz")
    async def _healthz():  # pragma: no cover
        return {"status": "ok"}

    @a.get("/metrics", response_class=PlainTextResponse)
    async def _metrics():  # pragma: no cover
        return "robot_backend_queue_depth 0\n"

    return a


# ------------------------------------------------------------------ V1: CORS 默认安全

class TestCorsDefaultSafe:
    """V1：未配置 `CORS_ALLOW_ORIGINS` → 响应**无** `Access-Control-Allow-Origin`。"""

    def test_no_cors_middleware_when_empty(self):
        """空配置 → 不挂中间件（不是"挂一个空 list"，行为与改动前逐字一致）。"""
        a = _app_with_routes()
        assert install_cors(a, "") == []
        # 浏览器面：跨域 GET 的响应里不该出现任何 CORS 头。
        with TestClient(a) as c:
            r = c.get("/robots", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}

    def test_whitespace_only_is_treated_as_empty(self):
        """只有空格/逗号也是"没配"—— 不能因为手滑多敲一个逗号就变成启用了。"""
        assert install_cors(_app_with_routes(), "  , ,, ") == []

    def test_real_app_default_has_no_cors_header(self):
        """生产 app（未设 CORS_ALLOW_ORIGINS 的默认路径）同样不放宽。

        这一条盯的是 app.main 的**装配**：即便 install_cors 写对了，
        若 main.py 忘调用/传了 `*`，此断言会红。
        """
        # conftest 只注入 JWT_SECRET，未设 CORS_ALLOW_ORIGINS → 期望默认关闭。
        with TestClient(real_app, raise_server_exceptions=False) as c:
            r = c.get("/healthz", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# ------------------------------------------------------------------ V2: CORS 可配置

class TestCorsConfigurable:
    """V2：配置指定 origin → 预检 `OPTIONS` 返回正确的 Allow-Origin / Allow-Methods。"""

    def test_preflight_returns_configured_origin(self):
        a = _app_with_routes()
        assert install_cors(a, "http://localhost:5173") == ["http://localhost:5173"]
        with TestClient(a) as c:
            r = c.options(
                "/robots",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization",
                },
            )
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert "GET" in r.headers["access-control-allow-methods"]
        assert r.headers["access-control-allow-credentials"] == "true"

    def test_unlisted_origin_is_not_allowed(self):
        """白名单外的 Origin 不得被回显 —— 只能回显配置里那条。"""
        a = _app_with_routes()
        install_cors(a, "http://localhost:5173")
        with TestClient(a) as c:
            r = c.get("/robots", headers={"Origin": "http://evil.example"})
        assert r.headers.get("access-control-allow-origin") != "http://evil.example"

    def test_multiple_origins_parsed(self):
        """逗号分隔 + 去空白：两项都要进白名单。"""
        a = _app_with_routes()
        origins = install_cors(
            a, " http://localhost:5173 , https://board.example.com "
        )
        assert origins == ["http://localhost:5173", "https://board.example.com"]

    def test_explicit_wildcard_path_allows_any_origin(self):
        """显式 `*` 才放行所有来源（这是**唯一**允许放行任意的路径）。"""
        a = _app_with_routes()
        assert install_cors(a, "*") == ["*"]
        with TestClient(a) as c:
            r = c.get("/robots", headers={"Origin": "http://anything.example"})
        assert r.headers["access-control-allow-origin"] == "*"


# ------------------------------------------------------------------ V3: 限流默认关闭 + 关键路径豁免

class TestRateLimitOffByDefault:
    """V3 前半：默认（env 关闭）不挂限流中间件，任何路径都不会 429。"""

    def test_disabled_returns_false_and_never_429(self):
        a = _app_with_routes()
        assert install_rate_limit(a, enabled=False, per_minute=1) is False
        with TestClient(a) as c:
            # 阈值 1 却连打 5 次仍全 200 —— 证明中间件确实没挂。
            codes = [c.get("/robots").status_code for _ in range(5)]
        assert codes == [200] * 5

    def test_invalid_threshold_does_not_enable(self):
        """阈值 <= 0 是配置错误：拒绝启用，绝不静默变成"所有请求 429"。"""
        a = _app_with_routes()
        assert install_rate_limit(a, enabled=True, per_minute=0) is False
        with TestClient(a) as c:
            assert c.get("/robots").status_code == 200


class TestRateLimitExemptPaths:
    """V3 后半（**关键**）：限流开启后，/healthz、/metrics、/internal/* 永不被限。"""

    @pytest.mark.parametrize("path", sorted(EXEMPT_PATHS))
    def test_exempt_paths_never_429(self, path: str):
        a = _app_with_routes()
        assert install_rate_limit(a, enabled=True, per_minute=2) is True
        with TestClient(a) as c:
            # 阈值 2，打 10 次：普通路径早该 429，豁免路径必须全 200。
            codes = [c.get(path).status_code for _ in range(10)]
        assert codes == [200] * 10, f"{path} 被限流拦下（R-17 §6 硬门槛）"

    def test_internal_prefix_exempt(self):
        """内部写入路径（/internal/*）免限流 —— 拦它会丢数据而非防滥用。"""
        a = _app_with_routes()

        @a.post(INTERNAL_PATH_PREFIX + "ingest")
        async def _ingest():  # pragma: no cover
            return {"ok": True}

        install_rate_limit(a, enabled=True, per_minute=2)
        with TestClient(a) as c:
            codes = [c.post(INTERNAL_PATH_PREFIX + "ingest").status_code for _ in range(10)]
        assert codes == [200] * 10


class TestRateLimitEnforced:
    """限流开启后，**对外 REST** 超限返回 429 + `{"detail": ...}`。"""

    def test_exceeding_returns_429_with_detail(self):
        a = _app_with_routes()
        install_rate_limit(a, enabled=True, per_minute=3)
        with TestClient(a) as c:
            codes = [c.get("/robots").status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3:] == [429, 429]
        with TestClient(a) as c:
            c.get("/robots")  # 不计，仅确保下方读到的仍是本次窗口内的一致响应
        # 单独验响应体：重建 app 避免窗口计数干扰。
        b = _app_with_routes()
        install_rate_limit(b, enabled=True, per_minute=1)
        with TestClient(b) as c:
            c.get("/robots")
            r = c.get("/robots")
        assert r.status_code == 429
        assert r.json() == {"detail": "rate limit exceeded"}
        assert int(r.headers["retry-after"]) >= 1

    def test_ws_scope_is_not_throttled(self):
        """WS 握手不属于对外 REST：长连接握手一次，限流拦它只会让前端重连风暴更糟。

        直接调中间件并断言它把 scope 透传给下游（不去真的握手 —— 那需要
        完整的 WS 路由与鉴权，属 test_ws_* 的覆盖范围）。
        """
        mw = RateLimitMiddleware(app=None, per_minute=1)
        assert mw._is_exempt("/ws/robots", "websocket") is True


# ------------------------------------------------------------------ V5: 不改既有契约

class TestRealAppContractUnchanged:
    """V5：挂载中间件后，既有端点契约与鉴权行为不变（默认路径抽查）。"""

    def test_healthz_shape_unchanged(self):
        with TestClient(real_app, raise_server_exceptions=False) as c:
            r = c.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        # main.py healthz 的既有字段（R-03 §4 契约，字段集见 schemas/api.py:HealthResponse）。
        for field in ("status", "db", "written", "flushes", "rows_written", "rows_submitted"):
            assert field in body, f"/healthz 缺字段 {field}"

    def test_metrics_unauthenticated_unchanged(self):
        """免鉴权白名单不变：/metrics 仍 200（订单 15）。"""
        with TestClient(real_app, raise_server_exceptions=False) as c:
            assert c.get("/metrics").status_code == 200

    def test_robots_still_requires_auth(self):
        """鉴权行为不变：未带 token 的 /robots 仍 401（订单 15）。"""
        with TestClient(real_app, raise_server_exceptions=False) as c:
            assert c.get("/robots").status_code == 401
