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
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request

from app.rules.base import Alert

log = logging.getLogger("notifier")

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

    def __init__(self, kind: str, url: str, min_severity: int = 3,
                 timeout_s: float = 5.0) -> None:
        if kind not in ("dingtalk", "wecom", "generic"):
            raise ValueError(f"unsupported webhook kind: {kind}")
        self.kind = kind
        self.url = url
        self.min_severity = min_severity
        self.timeout_s = timeout_s

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
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            raw = resp.read().decode("utf-8", "replace")
            code = resp.status
        # 钉钉/企微成功均返回 errcode:0 / errcode:0 & errmsg:ok，
        # HTTP 200 不等于投递成功，必须再看业务码。
        if code != 200:
            raise RuntimeError(f"webhook HTTP {code}: {raw[:200]}")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return  # 非 JSON 响应视为已投递（generic 场景）
        errcode = body.get("errcode", 0)
        if errcode not in (0, None):
            raise RuntimeError(f"webhook errcode={errcode}: {body.get('errmsg')}")

    async def send(self, alert: Alert) -> bool:
        """返回是否真正投递。异常一律吞掉并记日志 —— 通知失败不能拖垮告警引擎。"""
        if _SEV_ORDER.get(alert.severity, 0) < _SEV_ORDER.get(self.min_severity, 0):
            return False
        payload = self.build_payload(alert)
        try:
            # urllib 是阻塞的；丢到线程池，避免堵住 alert_engine 的事件循环
            await asyncio.to_thread(self._post_blocking, payload)
            log.info("notified %s -> %s", alert.robot_id, self.kind)
            return True
        except Exception:
            log.exception("webhook notify failed (%s): %s/%s",
                          self.kind, alert.robot_id, alert.rule_id)
            return False


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
