# 派工单：订单 12 — 让 CNB 自行下载上游开源源，并复核既有审计结论

> 触发方式：`post-issue-comment`（本文件**不含**触发词，避免重复触发构建）

---

## 0. 背景：一个必须纠正的疏漏

主代理在本机下载了 **14 个上游开源源**到 `external/`（`external/SOURCES.md` 有完整清单），
但 `.gitignore` 只放行了两个 md 文件：

```
external/*
!external/SOURCES.md
!external/INTEGRATION-PLAN.md
```

**后果**：你在 CNB 构建容器里**看不到这些源码**。
而此前多个派单（订单 07、09、10）都引用了 `external/<repo>/...` 下的具体文件，
例如 `external/aiomqtt/docs/reconnection.md` —— 对你而言那是**不存在的路径**。

订单 07 / 09 之所以仍然做对了，靠的是派单正文里转述的要点与你的既有知识，
**并非真的对照了上游源码**。这不稳。本单把源码真正交到你手上。

## 1. 任务 T1 — 在构建容器里下载参考源

仓库内已有脚本 `scripts/fetch-external-refs.sh`（幂等，已存在则跳过）：

```bash
bash scripts/fetch-external-refs.sh
```

会拉取 14 个源（约 59 MB，`--depth 1`），其中 `alertmanager` 用 sparse checkout 只取 `docs/`。
也可只拉需要的：

```bash
bash scripts/fetch-external-refs.sh aiomqtt alertmanager
```

**要求**：
- 执行后贴出 `du -sh external/*/` 的真实输出，确认落地
- 若容器网络无法访问 github.com，**如实报告**（写「未验证 + 所需条件」），不要假装下载成功

## 2. 任务 T2 — 用真实源码复核三条既有结论

下载完成后，**逐条打开源码核对**，并报告「一致 / 不一致 / 无法确认」：

### 复核 ①：aiomqtt 2.5.0 的 `__aexit__` 修复（对应订单 07 的 I-1）

- 打开 `external/aiomqtt/` 的 CHANGELOG 或 release notes
- 找 `2.5.0` 条目是否含 *"Let exceptions bubble up in `Client.__aexit__` instead of suppressing them"*
- 这决定了 `aiomqtt==2.0.1` 是否真会导致 MQTT 断线后**静默不重连**

### 复核 ②：`Client` reusable 表述（对应订单 07 的 I-2）

- 打开 `external/aiomqtt/docs/reconnection.md`
- 确认是否有 *"The `Client` context is designed to be reusable (but not reentrant)"*
- 并确认官方示例是否把 `Client(...)` 构造放在 `while True` **之外**

### 复核 ③：Alertmanager 的 inhibition 语义（对应订单 10 的 I-5）

- 打开 `external/alertmanager/docs/configuration.md`
- 找 `inhibit_rules`，确认三个概念：`source_match` / `target_match` / `equal`
- 这直接决定订单 10 的抑制实现是否正确

## 3. 任务 T3 — 若发现不一致，立即指出

若任何一条与 `external/INTEGRATION-PLAN.md` 或已合入代码的注释**不符**，
**明确指出并给出正确表述**。不要为了保持一致而附和。

已合入的相关代码（供你核对）：
- `backend_ingestion/app/services/mqtt_subscriber.py` — Client 复用 + 退避
- `backend_ingestion/requirements.txt` — `aiomqtt==2.5.1`

## 4. 验收标准

- [ ] 贴出 `bash scripts/fetch-external-refs.sh` 后的 `du -sh external/*/` 真实输出
- [ ] 三条复核**每条**给出结论：一致 / 不一致 / 无法确认
- [ ] 每条结论附**源码出处**（文件路径 + 引用原文片段）
- [ ] 若不一致，给出修正建议
- [ ] 若网络不通无法下载，**如实说明**，不要伪造

## 5. 禁止项

1. **禁止**伪造下载成功 —— 跑不通就写「未验证 + 所需条件」
2. **禁止**为了让结论好看而附和既有说法；发现不一致必须指出
3. **禁止**把 `external/` 下的源码提交进仓库（.gitignore 已排除，保持排除）
4. **禁止**修改 `scripts/fetch-external-refs.sh` 的源清单，除非发现仓库地址失效
   （已知变迁：`sbtinstruments/aiomqtt` → `empicano/aiomqtt`；
   `encode/uvicorn` → `Kludex/uvicorn`，脚本中已是新地址）
5. **禁止**只推分支就宣称完成 —— 尝试 push + 开 PR；因 `repo-code:rw` 失败则**原文报告**

## 6. 交付方式

1. 分支 `feat/order-12-external-refs-verify`（基于最新 `main`）
2. 主要交付物是**复核报告**，写在 issue 评论里；若有代码修正则一并提交
3. 尝试 push + 开 PR
4. push 失败则把完整报告写进 issue 评论

---

**定位：你是复核者，不是执行者。目标是把"我转述的要点"升级为"你亲眼看到的源码"。发现我说错了，直接说。**
