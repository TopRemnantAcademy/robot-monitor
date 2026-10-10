"""告警通知渠道（I-14 的"落到人"部分）。

设计约束（为什么这么写）：
1. **工厂有现成机器人吗？** —— 本仓无法知道。
   钉钉自建机器人 / 企微群机器人**都只暴露一个 HTTP webhook URL**，
   且两者的 payload 结构不同。故采用"渠道 = 适配器"模式：
   配置里填 URL，`kind` 决定 payload 形状。没有配置则整条链路静默跳过。
2. **不引入新依赖** —— `keep` / `prom2teams` 各自都带一整套运行时（且 keep 无钉钉/企微
   provider，实证 `keep/keep/providers/` 只有 slack/teams/telegram/webhook/ntfy）。
   这里只做一个转发器，用**标准库 urllib** 就够，避免为一个 webhook 拖进一个平台。
3. **密钥不进仓库**（AGENTS.md §5）—— URL 里含 token，只从环境变量读。
"""

from app.notifiers.webhook import WebhookNotifier, build_notifier

__all__ = ["WebhookNotifier", "build_notifier"]
