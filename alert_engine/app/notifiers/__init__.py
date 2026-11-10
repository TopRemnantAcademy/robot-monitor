"""Alert notification channels (the "reach a human" part of I-14).

Design constraints (why it is written this way):
1. Does the factory have an off-the-shelf robot? -- this repo cannot know.
   Both DingTalk self-built bots / WeCom group bots expose only a single HTTP webhook URL,
   and their payload structures differ. Hence the "channel = adapter" pattern:
   the config holds the URL, and `kind` decides the payload shape. With no config, the
   whole chain is silently skipped.
2. No new dependencies -- `keep` / `prom2teams` each bring a whole runtime (and keep has no
   DingTalk/WeCom provider; verified that `keep/keep/providers/` only has slack/teams/telegram/webhook/ntfy).
   Here we only need a forwarder, and the standard-library urllib suffices, avoiding
   pulling in an entire platform just for one webhook.
3. No secrets in the repo (AGENTS.md §5) -- the URL contains a token, read only from an env var.
"""

from app.notifiers.webhook import WebhookNotifier, build_notifier

__all__ = ["WebhookNotifier", "build_notifier"]
