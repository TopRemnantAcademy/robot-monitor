# 订单 34 — e2e 假绿专项：SKIP 静默变绿 + 镜像声明失效诊断修复（🔴→🟢）

## 定位

你是开发工程师，证据优先。**第一动作是复现与归因**，把下面的证据链走一遍再动手；归因结论写进 PR 描述。

## 基线

- 起点：`main`（crontab 已恢复：`c4252b3`+`4552492`，`build-crontab-sync` 已注册成功）
- 交付分支：`feat/order-34-e2e-real-green`

## 实证（2026-10-13 早上取证，不得跳过复读）

1. push 构建 `cnb-st7-1k4onp4ok`（sha 68b7b8b）的 e2e-30k-landing pipeline（id `-001`）：
   - stages：Prepare 20.4s / **e2e 仅 240ms** / End；整体 21s，status=success
   - 日志原文：`【E2E SKIP】镜像内 PATH 找不到 python3/python（当前镜像：缺省镜像）。`
   - 同一日志里 `docker 可用：Docker version 29.6.2` —— **docker 有、python 无**
2. `.cnb.yml`（main @ 4552492）三处 e2e 定义：
   - push 块（~line 195）：**已声明** `docker.image: python:3.11-slim`
   - pull_request 块（~line 379）：已声明
   - crontab 块（`"crontab: 0 3 * * *"`，~line 479）：**漏了 `docker.image`**（只有 services/env/stages）
3. 矛盾点（本单核心）：push 块明明声明了 image，e2e 却跑在缺省镜像里 —— **归因未明**。
   注意 68b7b8b 这次 push 被拆成了两个 build（test 与 e2e 各一个 SN），而更早的 push（e1e89d6）
   是单 build 双 pipeline —— 怀疑与订单 32 对 `.cnb.yml` 的改动（两级硬门禁）改变了 pipeline 分组/
   继承关系有关。**先诊断，后修复**。

## 必读

1. `.cnb.yml` 全文 —— 特别是 push / pull_request / 两个 crontab 块的差异
2. `tests/e2e/test_e2e_30k.py` 与 `tests/e2e/README.md` —— skip 语义（退出码 3 = 环境不具备）
3. `STATUS.md` §五「仍未验证」—— e2e 30k 至今没有一次真实落库数字
4. 订单 32 对 `.cnb.yml` 的 diff（`git show 77ec64c -- .cnb.yml`）—— 嫌疑人

## 任务分解

### T1 归因（先做，结论进 PR）

- 回答：为什么 push 块声明了 `docker.image: python:3.11-slim`，e2e stage 仍在缺省镜像跑？
  （对照 grammar 语义：`docker.image` 与 `services: [docker]` 并存时的行为；对照订单 32 前后
  push 构建的 pipeline 分组变化；给出 file:line 与日志双重证据）

### T2 修复

- push / pull_request / crontab **三处** e2e 必须真实跑在 python:3.11-slim
- 修复后 CI 的 e2e 必须**真跑**：日志出现 mock_fleet 起栈 + 落库行数断言（≥30000 的真实输出）
- 保留既有 skip 语义（真的没 docker 时明确 skip、exit 0），但 SKIP 输出前加横幅
  `【E2E SKIPPED —— 本构建未实跑 30k 落库】`，让「绿」不再能冒充实跑

### T3 证据归档

- 一次真跑的完整输出（行数、时长）归档进 `tests/e2e/README.md` 的「实跑记录」节
- `STATUS.md` §五「仍未验证」表同步更新（e2e 30k 从「未验证」改为「CI 承证 + 日期 + 数字」；
  若 T1/T2 后仍无法真跑，如实写明原因，**禁止伪装通过**）

## 验收标准（可判定）

- [ ] PR 描述含 T1 归因结论（证据 = file:line + 日志行）
- [ ] 三处 e2e 定义都带 `docker.image: python:3.11-slim`（或等效且经论证的方案）
- [ ] **你自己的 PR 的 CI 里，e2e stage 出现真实落库输出（非 SKIP）**，行数 ≥30000
- [ ] `STATUS.md`、`tests/e2e/README.md` 与实现一致
- [ ] 既有 pytest / npm-test / 三道新门禁不因本单变红
- [ ] `orders/34-dispatch.md` 一并提交进分支

## 禁止项

1. 禁止 `|| true` / `continue-on-error` / 把 SKIP 改成静默（可追溯是底线）
2. 禁止改 `pytest.ini` testpaths
3. 禁止为让 e2e 变绿而下调 `E2E_MIN_ROWS` 阈值
4. 禁止弱化既有测试断言
5. **产出文件白名单**：`.cnb.yml`、`tests/e2e/**`、`STATUS.md`、`docs/**`；其余文件出现在 diff 里即无效交付

## 交付方式

分支 `feat/order-34-e2e-real-green` → PR；描述含归因结论 + 真跑日志片段（行数/时长）。

## 失败上报

原文粘贴错误与 SKIP 原因；禁止静默失败。
