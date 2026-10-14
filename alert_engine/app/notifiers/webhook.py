"""通用 webhook 通知器：钉钉 / 企业微信 / 通用 JSON 三态适配。

为什么是"通用转发 + 三态适配"而不是直接引 `keep`：
- `keep` 是事件管理平台（活项目，`providers/` 实证含 slack/teams/telegram/webhook/ntfy），
  但**没有** dingtalk / wecom provider —— 引进来后仍要自己写这两个适配器，
  收益是"多一个平台要运维"。工厂只有 100 台，不值。
- 钉钉/企微群机器人的接入方式都是 **一个 HTTP POST + 固定 JSON 结构**，
  20 行代码的事；用标准库 `urllib` 即可，零新增依赖。

payload 形状（依据各家公开的群机器人文档，均为"text/markdown 消息"）：

  钉钉  : {"msgtype":"markdown","markdown":{"title":T,"text":T}}
  企微  : {"msgtype":"markdown","markdown":{"content":C}}
  通用  : {"robot_id":..,"rule_id":..,"severity":..,"message":..,"ts":..}  ← 原始字段直传

注意：钉钉 markdown 用 `text` 键，企微用 `content` 键 —— 写错会被对方 400，
故这里按 `kind` 分别构造，不做"猜字段"。

失败原因分类（R-15 / GAP-3）
---------------------------
投递失败此前**只进日志**，运维与 Prometheus 都无从知道"通知静默丢失"。
本模块把失败原因收敛成**封闭的 6 个 reason**（见 `NOTIFY_REASONS`），
由 `NotifyError.reason` 携带出去，供 `app/main.py` 打 `reason` 标签计数。
只分类、不改语义：`send()` 依旧**永不外抛**（通知失败不得拖垮告警引擎）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
import urllib.error
import urllib.request

from app.rules.base import Alert

log = logging.getLogger("notifier")

#: 通知失败的**封闭分类集合**。`app/main.py` 用它的取值打 Prometheus 的 `reason` 标签，
#: 故这里刻意保持小且穷尽 —— 标签基数失控（每个不同异常串一个标签）是 Prometheus
#: 反模式；而"分不清是网络不通还是 token 过期"则让告警规则无法给出动作。
#:
#: - `timeout`      : 连接/读取超时（对方限流、网络慢），**值得重试**
#: - `network`      : DNS / 连接被拒 / 连接中断（`URLError` 及无 `OSError` 兜底项），**值得重试**
#: - `http_error`   : HTTP 状态码非 2xx（4xx 为配置错，5xx 值得重试）
#: - `business_error`: HTTP 200 但业务码非 0（钉钉/企微 token 失效），**重试无用**
#: - `config`       : 本地尚未发出请求（payload 不可序列化等），重试无用
#: - `unknown`      : 兜底。**不掩盖**：未知原因照样计数，只是标签不可细分
NOTIFY_REASONS = (
    "timeout",
    "network",
    "http_error",
    "business_error",
    "config",
    "unknown",
)


class NotifyError(Exception):
    """投递失败，带**可分类的 reason**（R-15 T4）。

    为什么用异常携带 reason 而不是让 `send()` 返回 reason 字符串：
      `send()` 的返回类型是既有契约（`bool`，见 tests/test_notifier.py 的
      `assert ... is False` 系列），改签名会波及所有调用方与既有用例。
      异常是本模块内部从 `_post_blocking`（同步、在线程池里跑）跨到 `send()`
      （async）传递细粒度信息的**零签名改动**通道；`send()` 捕获后仍翻译回 bool。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        # 非白名单 reason 归一到 unknown：防止后续有人在别处 `raise NotifyError("随便写")`
        # 把高基数标签带进 Prometheus。
        self.reason = reason if reason in NOTIFY_REASONS else "unknown"
        self.detail = detail
        super().__init__(f"{self.reason}: {detail}" if detail else self.reason)


def classify_exception(exc: BaseException) -> str:
    """把任意异常映射到 `NOTIFY_REASONS` 之一。

    顺序敏感：`socket.timeout` / `ssl.SSLError` 都是 `OSError` 的子类，
    必须在 `OSError` 之前判；`urllib.error.HTTPError` 又是 `URLError` 的子类，
    必须在 `URLError` 之前判 —— 写反了会让"token 过期(401)"被误标成 `network`，
    而这两者的运维动作完全不同（前者改配置，后者查网络）。
    """
    if isinstance(exc, NotifyError):          # 已带分类，直接透传
        return exc.reason
    if isinstance(exc, urllib.error.HTTPError):
        # 4xx/5xx 都归 http_error。理由：两者在**重试策略**上同层（都由 `_send_with_retry`
        # 决定），细分收益低于标签基数成本；而响应体本身由 webhook.py 的
        # `log.exception` 完整打出，需要看详情的人去日志里看。
        # ⚠️ 必须判在 `URLError` 之前 —— `HTTPError` 是它的子类。
        return "http_error"
    if isinstance(exc, (TimeoutError, socket.timeout, ssl.SSLError)):
        # 注意：`TimeoutError` 就是 `socket.timeout`（3.10+ 起是内建别名），保留写法
        # 只为显式表达意图；`ssl.SSLError` 从 3.11 起继承 OSError 而非 ConnectionError。
        return "timeout" if not isinstance(exc, ssl.SSLError) else "network"
    if isinstance(exc, urllib.error.URLError):
        # URLError.reason 常见为 ConnectionRefusedError / socket.gaierror(DNS)。
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            return "timeout"
        return "network"
    if isinstance(exc, (ConnectionError, socket.gaierror)):
        return "network"
    if isinstance(exc, OSError):
        return "network"
    if isinstance(exc, (TypeError, ValueError)):
        # json.dumps 失败 / 非法 url 等：请求根本没发出去，重试等同重放同一个错误。
        return "config"
    return "unknown"


# severity → 中文标签（大屏与通知里保持同一套词）
_SEV_LABEL = {1: "提示", 2: "预警", 3: "严重"}

# 只有达到该级别的告警才推送，避免"低电量风暴"刷屏。
# 依据：本项目 severity 语义 1 info / 2 warning / 3 critical（infra/init.sql:60）。
_SEV_ORDER = {1: 0, 2: 1, 3: 2}


class WebhookNotifier:
    """把 Alert 转成各渠道 JSON 并 POST 出去。

    `kind`: "dingtalk" | "wecom" | "generic"
    `min_severity`: 低于此值不推送（默认 3，即只推 critical）。
    """

    #: 最近一次 `send()` 的失败原因（成功或未发送时为 None）。
    #: 无标签默认值：实例属性而非类属性，避免多实例互相串。
    last_reason: str | None = None

    def __init__(self, kind: str, url: str, min_severity: int = 3,
                 timeout_s: float = 5.0) -> None:
        if kind not in ("dingtalk", "wecom", "generic"):
            raise ValueError(f"unsupported webhook kind: {kind}")
        self.kind = kind
        self.url = url
        self.min_severity = min_severity
        self.timeout_s = timeout_s
        self.last_reason = None

    # ------------------------------------------------------------- payload
    def _title(self, alert: Alert) -> str:
        return f"[{_SEV_LABEL.get(alert.severity, '未知')}] {alert.rule_id} · {alert.robot_id}"

    def build_payload(self, alert: Alert) -> dict:
        title = self._title(alert)
        body = f"{title}\n\n{alert.message}"
        if alert.detail:
            # detail 是可选的规则上下文（如关节号/温度值），压成一行便于手机阅读
            body += "\n\n" + json.dumps(alert.detail, ensure_ascii=False,
                                         separators=(",", ":"))

        if self.kind == "dingtalk":
            return {"msgtype": "markdown", "markdown": {"title": title, "text": body}}
        if self.kind == "wecom":
            return {"msgtype": "markdown", "markdown": {"content": body}}
        # generic：原始字段直传，交给工厂自建的转发服务自行加工
        return {
            "robot_id": alert.robot_id,
            "rule_id": alert.rule_id,
            "severity": alert.severity,
            "message": alert.message,
            "detail": alert.detail,
            "ts": None,
        }

    # --------------------------------------------------------------- send
    def _post_blocking(self, payload: dict) -> None:
        """同步 POST。失败一律抛 `NotifyError`（带 reason），**不外泄原始异常**。

        为什么要在这里就翻译成 NotifyError：调用方（`send`）跑在事件循环里，
        它只关心"什么原因失败了"，不该再重复一遍 urllib/socket 的 isinstance 阶梯。
        分类逻辑集中在 `classify_exception()`，一处可测。
        """
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            # payload 不可序列化 —— 本地问题，请求未发出；重试等于重放同一个错误。
            raise NotifyError("config", f"payload not serializable: {exc}") from exc

        req = urllib.request.Request(
            self.url, data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", "replace")
                code = resp.status
        except NotifyError:                       # pragma: no cover - 防御性
            raise
        except Exception as exc:
            # HTTPError / URLError / socket 异常全在这条路上；分类后再抛。
            raise NotifyError(classify_exception(exc), f"{type(exc).__name__}: {exc}") from exc

        # 钉钉/企微成功均返回 errcode:0 / errcode:0 & errmsg:ok，
        # HTTP 200 不等于投递成功，必须再看业务码。
        if code != 200:
            # urlopen 对 4xx/5xx 会抛 HTTPError（不走这里），故本分支只在自定义
            # handler 或非标准响应下命中；仍按 http_error 分类。
            raise NotifyError("http_error", f"webhook HTTP {code}: {raw[:200]}")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return  # 非 JSON 响应视为已投递（generic 场景）
        errcode = body.get("errcode", 0)
        if errcode not in (0, None):
            # 业务码非 0 = token 失效 / 被限流：重试无用（同一 token 仍会被拒）。
            raise NotifyError("business_error",
                              f"errcode={errcode}: {body.get('errmsg')}")

    async def send(self, alert: Alert) -> bool:
        """返回是否真正投递。异常一律吞掉并记日志 —— 通知失败不能拖垮告警引擎。

        契约不变（`bool`，永不外抛）。新增能力是副作用：失败时把**可分类的 reason**
        写进日志，供上层的 `robot_alert_notify_failed_total{reason}` 计数。
        分类必须在这里落定 —— `WebhookNotifier` 是唯一知道"为什么失败"的人，
        上层只拿到一个 bool 的话，标签只能填 unknown。
        """
        if _SEV_ORDER.get(alert.severity, 0) < _SEV_ORDER.get(self.min_severity, 0):
            return False
        try:
            payload = self.build_payload(alert)
        except Exception as exc:
            # 构造 payload 就炸（detail 含不可 JSON 化对象等），同样不外抛。
            log.exception("webhook notify failed (%s): %s/%s",
                          self.kind, alert.robot_id, alert.rule_id)
            self.last_reason = classify_exception(exc)
            return False

        try:
            # urllib 是阻塞的；丢到线程池，避免堵住 alert_engine 的事件循环
            await asyncio.to_thread(self._post_blocking, payload)
            log.info("notified %s -> %s", alert.robot_id, self.kind)
            self.last_reason = None
            return True
        except Exception as exc:
            # ⚠️ 这行 `log.exception`（本文件历史上的 `:115`）是**既有契约**，
            # R-15 §6 明令禁止删除；本单只在它之后补分类，不动它。
            log.exception("webhook notify failed (%s): %s/%s",
                          self.kind, alert.robot_id, alert.rule_id)
            # 为什么把分类结果存到实例上而不是返回值里：见 NotifyError 的 docstring
            # —— `send()` 的返回类型是对外契约。实例属性是"最近一次投递结果"的
            # 只读旁注，不改变调用协议；`app/main.py` 失败后立即读取。
            self.last_reason = classify_exception(exc)
            return False


__all__ = ["WebhookNotifier", "NotifyError", "NOTIFY_REASONS",
           "classify_exception", "build_notifier"]


def build_notifier(settings) -> WebhookNotifier | None:
    """从配置装配通知器；未配置 URL 时返回 None（链路整体跳过）。"""
    url = getattr(settings, "alert_webhook_url", "") or ""
    if not url.strip():
        return None
    return WebhookNotifier(
        kind=getattr(settings, "alert_webhook_kind", "generic"),
        url=url.strip(),
        min_severity=int(getattr(settings, "alert_webhook_min_severity", 3)),
    )
