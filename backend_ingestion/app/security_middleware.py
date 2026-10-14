"""CORS + 可选限流（R-17，来源 docs/GAP-SCAN-2026-10-13.md GAP-4）。

## 为什么需要这个文件

`main.py` 全文此前 grep `cors|CORSMiddleware|rate.?limit|throttle|slowapi|add_middleware`
**0 命中**（R-17 §1 实证）。现有防护只有 JWT 鉴权。两类真实风险：

1. **CORS 缺失**：前端与后端**不同源**部署（前端独立域名/CDN，或本地联调
   `:5173` 打 `:8000`）时，浏览器会拦掉 REST 与 WS 握手。失败发生在浏览器侧，
   服务端日志一片干净 —— 排查成本最高的那类问题。
2. **无限流**：对外接口无速率限制。内网可信拓扑尚可接受，一旦对外暴露
   （含 `/auth/jwt/login`），没有暴力破解/刷接口的基础防护。

## 安全默认：关闭 / 最小暴露（R-17 §2 §6 硬约束）

- **CORS 默认不启用**：`CORS_ALLOW_ORIGINS` 留空 → **不挂** `CORSMiddleware`，
  响应里不会出现 `Access-Control-Allow-Origin`。绝不默认 `allow_origins=["*"]`：
  浏览器规范下 `*` 与 `allow_credentials=True` 组合不被允许，一旦有人为了"让它能跑"
  改成回显 Origin + 凭据，等于把 CSRF/信息泄露面从"同源"扩到"任意站点"。
  只有显式写 `CORS_ALLOW_ORIGINS=*` 才放行所有来源（代码里单独分支 + 风险告警日志）。
- **限流默认关闭**：`RATE_LIMIT_ENABLED=false`。默认即"不加任何限流逻辑"，
  既有 e2e / 探针 / 监控的请求节奏逐字不变（R-17 §6 硬门槛）。

## 限流为什么不用 slowapi

R-17 §3 要求优先用标准库或现有栈自研，并评估新依赖体积/维护成本。
`slowapi` 是一条 `limits` + `decorator` 依赖链，且它的主用法是**逐路由装饰器** ——
那会改动路由处理函数（R-17 §3 明确"不改路由处理函数"）。这里用 ASGI 中间件
+ 进程内固定窗口计数器实现，**零新依赖**，且天然的"只加中间件、不碰路由"。

## 限流的豁免面（R-17 §6 硬门槛，一条都不能少）

- `/healthz`、`/metrics`：被 CI/e2e/容器编排/Prometheus 以秒级频率依赖。
  限流拦掉它们 = 探针判死重启、监控静默失效（订单 15 已把这两个定为免鉴权白名单，
  这里必须同样免限流）。
- `/ws/robots`：WS 是长连接、握手只有一次，限流无意义且会打断前端实时流。
- 内部写入路径：`/internal/*` 前缀（预留的内部写入口径，见 `INTERNAL_PATH_PREFIX`）。

## 超限响应格式

`429 Too Many Requests` + `{"detail": "rate limit exceeded"}` —— 与既有错误响应
（FastAPI 默认 `{"detail": ...}` / `main.py` 的 503 `{"detail": ...}`）同构。
带 `Retry-After` 头，单位秒（RFC 9110 §10.2.3）。
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("security_middleware")

# 内部写入路径前缀。R-17 §4 T2 要求「必须排除内部写入路径」—— 这些路径是
# 服务间调用（可靠、高频、非用户驱动），限流拦掉它们会丢数据而不是防滥用。
# 当前仓库尚无 /internal/* 路由，此处按前缀预留：一旦将来挂上内部写入端点，
# 不必再回来改限流白名单（避免"新端点被限流"这种上线后才发现的问题）。
INTERNAL_PATH_PREFIX = "/internal/"

# 永远免限流的路径（精确匹配）。见模块头「限流的豁免面」。
EXEMPT_PATHS = frozenset({"/healthz", "/metrics"})


def _parse_origins(raw: str) -> list[str]:
    """`CORS_ALLOW_ORIGINS` 逗号分隔 → 去空白、丢空项后的列表。

    唯一的特殊值是 `*`（显式放行所有来源）。除此之外**不解析、不推断** ——
    用户写什么就是什么，避免"帮用户把 `example.com` 补成 `https://example.com`"
    这种自作聪明（补错了更危险：用户以为配了 A，实际放行了 B）。
    """
    return [item.strip() for item in raw.split(",") if item.strip()]


def install_cors(app: FastAPI, raw_origins: str) -> list[str]:
    """按 `raw_origins` 挂载 CORS 中间件。空 → 不挂，返回 []。

    返回实际生效的 origins（供调用方日志/自检），**不返回**则调用方无法区分
    "配置为空所以没挂"与"代码忘了挂"。
    """
    origins = _parse_origins(raw_origins)
    if not origins:
        # 默认路径（未配置）—— 刻意**不挂**中间件，而不是挂一个 allow_origins=[]：
        # 空列表的中间件对预检会返回 400（无 Allow-Origin），行为上是另一种
        # "拒绝"，但它仍然会拦截 OPTIONS 请求并改变错误形态。不挂 = 与改动前逐字一致。
        log.info("CORS disabled (CORS_ALLOW_ORIGINS empty) — same-origin only")
        return []

    wildcard = origins == ["*"]
    if wildcard:
        # 显式 `*`：用户明确要求放行所有来源。风险提示不可少 —— 这正是 R-17 §4 T1
        # 要"在注释/文档中标注其风险"的那条路径。告警级日志让它在生产日志里可见。
        log.warning(
            "CORS: CORS_ALLOW_ORIGINS='*' — allowing ALL origins (explicitly configured). "
            "This widens CSRF/data-exposure surface; restrict to known frontend origins in production."
        )
    else:
        log.info("CORS enabled for %d origin(s): %s", len(origins), origins)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        # 最小可用集（R-17 §4 T1）：
        # - credentials=True：前端用 `Authorization: Bearer <JWT>`（订单 15），
        #   这不是 cookie 凭据，但浏览器在跨域 XHR 上仍要求 Allow-Credentials
        #   （带 Authorization 头的跨域请求走 preflight，响应缺该头会被拦）。
        #   ⚠️ 正因为开了它，**更不能**默认 `*`：`*` + credentials 既是规范禁止的
        #   组合，也是"任意站点都能带着用户凭据打后端"的经典事故形态。
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    return origins


# ---------------------------------------------------------------------------
# 可选限流（默认关闭）
# ---------------------------------------------------------------------------

class RateLimitMiddleware:
    """进程内固定窗口限流（ASGI 层，零依赖）。

    【为什么是固定窗口而非滑动窗口/令牌桶】
    这是**滥用防护的最低档**（R-17 §1：内网可信拓扑下"尚可接受"，对外暴露时兜底），
    不是精确配额。固定窗口的边界突刺（窗口切换瞬间可打 2× 配额）在这个用途下
    可接受，而它换来的是：无锁、无后台任务、无内存增长（每个 key 只存两个整数）。

    【为什么按客户端 IP 计数】
    鉴权发生在**路由依赖**里（`current_active_user`），中间件跑在它之前，拿不到
    用户身份。IP 是这一层能拿到的最接近"滥用者"的标识。代价是 NAT 后面的多用户
    共享配额 —— 与"默认关闭"的定位一致：开启者应知道自己在做粗粒度防护。

    【内存有界】
    `_hits` 每个 (窗口, key) 存一条。窗口切换时该 key 的旧记录被覆盖写，但
    **从未出现过的 key 不会被回收** —— 若长期运行且被低频扫 IP，字典会缓慢增长。
    这里的取舍：中间件实例是进程级的，单进程日志中 IP 数量有限（工厂内网）；
    真要对外暴露，应把限流下沉到网关（R-17 §3 明确"不引入重量级网关"是本单边界，
    网关方案不在本单范围）。为控风险，`_sweep()` 在每个窗口切换时清掉陈旧条目。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        per_minute: int,
        exempt_paths: frozenset[str] = EXEMPT_PATHS,
        internal_prefix: str = INTERNAL_PATH_PREFIX,
    ) -> None:
        self.app = app
        self.per_minute = per_minute
        self.exempt_paths = exempt_paths
        self.internal_prefix = internal_prefix
        self._window = 60.0
        self._window_start = 0.0
        self._hits: dict[str, int] = defaultdict(int)

    def _client_key(self, scope: Scope) -> str:
        """客户端标识 = 直连地址。

        刻意**不信任** `X-Forwarded-For`：该头可被客户端任意伪造，用它做限流 key
        等于攻击者换一个头值即可绕过。只有在明确部署在可信代理（并知道代理会覆写
        该头）时才应启用 XFF —— 那属于网关注册范畴，不在本单边界内。
        """
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _is_exempt(self, path: str, scope_type: str) -> bool:
        # WS 握手不计限流：长连接只握手一次，限流拦掉它/让前端重连风暴更糟。
        # （scope_type == "websocket" 的分支在 __call__ 里提前返回，这里只做防御性判断。）
        if scope_type == "websocket":
            return True
        if path in self.exempt_paths:
            return True
        if path.startswith(self.internal_prefix):
            return True
        return False

    def _check(self, key: str, now: float) -> bool:
        """返回 True=放行、False=超限。顺带完成窗口翻转与陈旧条目清扫。"""
        if now - self._window_start >= self._window:
            # 窗口翻页：开启新窗口。旧窗口的计数不再有意义，直接换新字典 ——
            # 这同时是内存回收点（上面 __init__ 注释里的"陈旧条目"在此清掉）。
            self._window_start = now
            self._hits = defaultdict(int)
        self._hits[key] += 1
        return self._hits[key] <= self.per_minute

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # lifespan 是 ASGI 的第三个 scope 类型（uvicorn 用它跑 startup/shutdown），
        # 必须放行 —— 拦掉它进程起不来。
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if self._is_exempt(path, scope["type"]):
            await self.app(scope, receive, send)
            return

        now = time.monotonic()
        if self._check(self._client_key(scope), now):
            await self.app(scope, receive, send)
            return

        # 超限。响应体格式与既有错误响应同构（{"detail": ...}），
        # 前端 401 拦截器之外再挂一个 429 处理即可，不需要另学一种结构。
        retry_after = max(1, int(self._window - (now - self._window_start)) + 1)
        log.warning("rate limit exceeded: path=%s", path)
        response = JSONResponse(
            {"detail": "rate limit exceeded"},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
        await response(scope, receive, send)


def install_rate_limit(app: FastAPI, *, enabled: bool, per_minute: int) -> bool:
    """按 env 挂载限流中间件。返回是否真的启用（供调用方日志）。"""
    if not enabled:
        log.info("rate limit disabled (RATE_LIMIT_ENABLED=false) — no throttling")
        return False
    if per_minute <= 0:
        # 阈值非正数等于"全拒"，是配置错误而非用户意图。拒绝启用并告警，
        # 绝不静默变成"所有请求 429"（那比不限流危险得多）。
        log.error(
            "RATE_LIMIT_PER_MINUTE=%d is invalid (must be > 0) — rate limit NOT enabled",
            per_minute,
        )
        return False
    app.add_middleware(RateLimitMiddleware, per_minute=per_minute)
    log.info(
        "rate limit enabled: %d req/min per client IP; exempt=%s, internal_prefix=%s",
        per_minute,
        sorted(EXEMPT_PATHS),
        INTERNAL_PATH_PREFIX,
    )
    return True
