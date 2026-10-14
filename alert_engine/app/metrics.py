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
    "被 inhibition 抑制的告警条数（订单 10 的 offline 层 + 订单 30 的 severity 层）",
    ["rule_id", "reason"],
    registry=registry,
)
alerts_silenced = Counter(
    "robot_alert_silenced_total",
    "被 silence 静默（TTL 生效期内）而未落库/未发布的告警条数（订单 17）",
    ["rule_id"],
    registry=registry,
)
alerts_escalated = Counter(
    "robot_alert_escalated_total",
    "被升级重发的告警条数（severity=3 超时未 ack，订单 17）",
    ["rule_id"],
    registry=registry,
)
# 在线台数（订单 32 / ISSUE #100 改名）：原名 `robot_alert_robots_online`。
#
# 为什么改名：`_gauge` 后缀是**类型自证**，不是装饰。同名问题真实发生过 ——
# ISSUE #72 的起因就是「同一个在线台数」在两个进程里各有一个名字
# （后端 `robot_backend_robots_online`、告警引擎 `robot_alert_robots_online`），
# 面板/告警要维护两套 PromQL，且**没人能从名字判断哪个是 Gauge、哪个是 Counter**。
# 本次只改告警引擎侧：后端侧的名字被现网 PromQL 与 `rules/robot-alerts.yml` 引用，
# 改它属跨单返工（订单 32 禁止项 5）。加 `_gauge` 后，抓取端与面板作者
# 一眼可辨「这只可增可减、禁止对它用 rate()」，两个名字的差异也变得显式可查。
#
# 判据来源（阈值内 `last_seen`）见 `alert_engine/app/main.py:252` 的 ticker；
# 分子/分母口径（在线 / 固定 100）由 ticker 每秒刷新。
robots_online = Gauge(
    "robot_alert_robots_online_gauge",
    "ticker 线当前判定为在线的机器人数（分母固定 100）",
    registry=registry,
)
#: 告警**写库失败**的条数（`increase(...[5m]) > 0` 即触发）。
#:
#: 为什么必须单独埋这一颗：这条链路上 `robot_alert_emitted_total` 只统计
#: "去重后决定发出"，`store.save()` 的 insert 异常此前**只进日志**。
#: 于是「DB 无记录」与「告警根本没触发」在监控上完全同形 ——
#: 看板照常变绿，审计对账却少数据（G-4）。有了它，二者才可区分：
#: emitted 在涨而 store_errors 也在涨 ⇒ 告警确实判定出来了，只是没落库。
store_errors = Counter(
    "robot_alert_store_errors_total",
    "robot_alert 写库失败的告警条数（>0 即「有告警但 DB 里查不到」，看板绿是假的）",
    registry=registry,
)

#: 通知**投递成功**的条数（R-15 T1）。与 `notify_failed` 配对算失败率。
#:
#: 为什么成功也要计数：只有失败计数时，看到 `notify_failed_total = 37` 无法判断
#: 严不严重 —— 是 1% 丢包还是 100% 全丢？分母（分母 = sent + failed）必须存在，
#: `rate(notify_failed[5m]) / (rate(sent[5m]) + rate(notify_failed[5m]))` 才有意义。
notify_sent = Counter(
    "robot_alert_notify_sent_total",
    "成功投递到群机器人的告警条数（与 notify_failed_total 配对算投递失败率）",
    ["kind"],
    registry=registry,
)

#: 通知**投递失败**的条数，按 `reason` 细分（R-15 / GAP-3 的核心交付）。
#:
#: 为什么必须有这颗：通知失败此前**只进日志**。webhook 配错 / 钉钉企微限流时，
#: 告警包已落库并广播，值班却在群里收不到 —— 而 Prometheus 侧一无所知
#: （既有 `alerts_emitted` 只统计"决定发出"，与投递成败无关）。
#: 于是「通知静默丢失」在监控上与「通知正常」完全同形，看板永远绿。
#:
#: `reason` 取值是**封闭集**（见 `notifiers/webhook.py:NOTIFY_REASONS`）：
#: `timeout` / `network`（值得重试，查网络）、`http_error` / `business_error`
#: （多为配置错，查 token）、`config`（本地未发出）、`unknown`（兜底不掩盖）。
#: 维度刻意收窄 —— 每个不同异常串一个标签会让 Prometheus 时间序列数失控。
notify_failed = Counter(
    "robot_alert_notify_failed_total",
    "告警通知投递失败条数（按 reason 分类；>0 即「告警在库但群里没人收到」）",
    ["reason"],
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
