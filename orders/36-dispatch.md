# 订单 36（G-7）— 多机器人任务级仿真验证（AgentVerse 驱动 mock_fleet）

- **Issue**：#114（G-7）
- **基线**：`main`（含订单 28–35）
- **交付分支**：`feat/g7-agentverse-sim`
- **角色**：开发工程师（证据优先）
- **结论**：AgentVerse 路线**判不可行**（LLM 文本框架，不产遥测）；任务级仿真由既有
  `simulator/task_sim.py`（Issue #63）承担，已补自动化验收测试。

---

## 1. 任务与纪律

| 项 | 要求 |
| :--- | :--- |
| 验证优先 | 第一动作查清 `mock_fleet.py` 与 `external/` AgentVerse 物料现状，实测驱动能力 |
| 不可行不造 | 不可行则原文贴证据（报错/限制），写明「G-7 暂挂」，禁止伪造 harness |
| 白名单 | `simulator/**`、`external/`（只读）、`tests/**`、`docs/**`、`orders/36-dispatch.md` |
| 禁止项 | 不改 `backend_ingestion/`、`alert_engine/`、`frontend_dashboard/`；既有断言零改动 |

## 2. 现状核查（实证）

- `external/` 下**无** AgentVerse 源码，仅 4 个元数据文件；`.gitignore` 明确 `external/*` 不入库。
- `docs/UPSTREAM.md:75` 记 AgentVerse「`f90c4bd9` / 31 MB / 多机器人任务求解框架（LLM，备选）」；
  `STATUS.md:98` 记「已下载（未验证能否驱动 100 agent）」。
- 本次重新 `git clone --depth 1 https://github.com/OpenBMB/AgentVerse`，HEAD `f90c4bd`（38 MB），
  与 UPSTREAM 记录一致。

## 3. 实测证据（详见 `docs/agentverse-verification/README.md`）

| 探针 | 结果 |
| :--- | :--- |
| 无 `OPENAI_API_KEY` 时 `import agentverse` | ❌ `ImportError: cannot import name 'DEFAULT_CLIENT' from 'agentverse.llms.openai'` |
| 有 key 占位时构造 100 个 agent | ✅ 成功（0.002s，`ConversationAgent` + `OpenAIChat`） |
| agent `step()` 产物是否含遥测字段 | ❌ 无（`Message` 字段：`content/receiver/sender/sender_agent/tool_response`） |
| 给可用的本地 LLM 后是否产遥测 | ❌ 仍只有文本 `Message`，遥测字段命中为 0 |

根因：`agentverse/agents/base.py:21` 强制 `llm: BaseLLM`；`agentverse/llms/openai.py:37-58`
的 `DEFAULT_CLIENT` 只在有 key 分支定义 → 无 key 时 import 期即崩；
其 `step()` 是 LLM 文本对话，与 `mock_fleet` 的 17 字段数值 payload 不同层。

## 4. 处置

1. AgentVerse 不引入（生态代价：钉 pydantic v1 / openai 1.1.0 / fastapi 0.95.1，与本仓 v2 栈冲突；
   且 10Hz × 100 台的 LLM 往返延迟数量级不符）。
2. 任务级仿真由 `simulator/task_sim.py` 承担（Issue #63，已在 main）：
   调度器让遥测在时间上相关（pipeline 相邻工位反相关 / fault 中枢-邻居正相关），
   复用 `mock_fleet` 契约与 MQTT 发布器。
3. 新增 `tests/test_task_sim_harness.py`：把 G-7 的「≥10 台协同 + 相关性显著 + 契约一致」
   固化为可重复断言。

## 5. 交付物

| 文件 | 说明 |
| :--- | :--- |
| `simulator/agentverse_probe/verify_agentverse.py` | 三探针实证脚本（可复现） |
| `simulator/agentverse_probe/verify_with_local_llm.sh` | 排除「只是没 key」的对照实验 |
| `simulator/agentverse_probe/verify_agentverse_report.json` | 机器可读证据 |
| `docs/agentverse-verification/README.md` | 完整实证报告 |
| `tests/test_task_sim_harness.py` | 任务级 harness 自动化验收（5 用例） |
| `orders/36-dispatch.md` | 本单 |

## 6. 验收

- [x] `python -m pytest -q`：**126 passed, 1 skipped**（含新增 5 用例；后端/告警/前端主业零改动）
- [x] AgentVerse 驱动结论有实证（原始报错 + JSON 报告），非推测
- [x] 任务级 harness 实测 ≥10 台协同（12 台 r=−0.901 / fault r=+0.991，退出码 0）
- [x] 既有测试断言零改动、零弱化（`git diff --stat` 对既有目录为空）
