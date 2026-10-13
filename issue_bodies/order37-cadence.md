# 配额治理：crontab 降频 1×/天 + 积分保护，规避 AI 积分耗尽（402）

## 背景（实证）
- 2026-10-13 12:00 CST crontab 自主构建 `cnb-bd0-1k4pmljt2` 在 `pm-call` 阶段 `error`，runner 日志实锤：`[12:00:40] [llm error] HTTP 402 — [EXCEEDING_LIMIT] AI 积分已用尽，请联系根组织管理员提升配额`。
- 复活后首轮（cnb-6pq）虽无 402，但 4×/day 的 7 段流水线（pm/architect/dev/review/test/compliance/integration）共同消耗 AI 积分，实证撑不住 4 轮/日，402 在多数 crontab 轮次复发。

## 需求
1. 自主 crontab 流水线不得再因配额耗尽产生 `error` 构建（不再每 6h 红一条 402）。
2. 把白天配额让给高价值的人工 `work-mode` 派单（定向打缺口），自主巡检仅保留必要频度。

## 实施（维护者直接改，非 NPC 跑）
- (1) `.cnb.yml` crontab 调度 `0 0,6,12,18 * * *` → `0 18 * * *`（1×/天，18:00 CST 夜间一扫）。
- (2) `spec/ci/pm-call.md` 加 **step 0 积分保护**：启动先 `cnb build get-build-logs --createTime <今天>` 查近 6h 是否有 `status: error` 且日志含 `402`/`EXCEEDING_LIMIT`/`AI 积分已用尽` 的构建；若有则打印 `[QUOTA] AI 积分耗尽，跳过本轮，不调 LLM` 直接结束。该检查是 CNB API 调用、不耗 AI 积分。

## 验收
- (a) 下次 crontab 轮（18:00 CST）若配额仍紧，应打印 `[QUOTA] 跳过本轮` 而非 402 `error`；
- (b) 即便配额充裕，频率降为 1×/天，配额消耗较 4×/天降约 75%；
- (c) `build-crontab-sync --branch main` 返回 successfully synchronized，且 `git ls-remote` 实证远程 main 含本改动。

## 产出文件白名单
仅 `.cnb.yml`、`spec/ci/pm-call.md`。白名单外 diff 即无效交付。若仍需 root 组织管理员提额方能常态多轮，属配额硬上限、非代码缺陷。