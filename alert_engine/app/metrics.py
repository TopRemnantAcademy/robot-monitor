"""告警引擎的 Prometheus 指标暴露。

为什么告警引擎需要**独立**的 metrics server：
  `alert_engine/app/main.py` 是一个纯 asyncio 进程（`asyncio.run(amain())`），
  它**不跑任何 HTTP 框架** —— 没有 FastAPI，也没有 uvicorn。
  所以不能像后端那样在 FastAPI 上挂 `/metrics` 路由，
  必须起一个极小的 HTTP 服务把 `prometheus_client` 的输出吐出去，
  否则 `prometheus.yml` 里 `robot_alert_engine` 这个 job 永远 `up=0`
  → `AlertEngineDown` 规则**永久误报**。

实现用标准库 `http.server` 起在独立线程：
  - 不引入 aiohttp/uvicorn 等新依赖（保持告警引擎依赖面最小）
  - 不与 asyncio 事件循环争抢（`ThreadingHTTPServer` 单 worker，只读 registry）
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)
from prometheus_client.exposition import CONTENT_TYPE_LATEST

log = logging.getLogger("metrics")

registry = CollectorRegistry()

alerts_emitted = Counter(
    "robot_alert_emitted_total",
    "成功发出（落库+发布）的告警条数",
    ["rule_id"],
    registry=registry,
)
alerts_suppressed = Counter(
    "robot_alert_suppressed_total",
    "被 inhibition 抑制的告警条数（orders/10-dispatch.md 的抑制层）",
    ["rule_id"],
    registry=registry,
)
robots_online = Gauge(
    "robot_alert_robots_online",
    "ticker 线当前判定为在线的机器人数（分母固定 100）",
    registry=registry,
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib 命名约定)
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = generate_latest(registry)
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静默：避免每次抓取刷日志
        return


def start(port: int = 8001) -> ThreadingHTTPServer:
    """在后台线程启动 metrics HTTP 服务，返回 server 以便关闭。"""
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)  # noqa: S104
    t = threading.Thread(target=server.serve_forever, name="metrics-http", daemon=True)
    t.start()
    log.info("alert_engine metrics on :%d/metrics", port)
    return server
