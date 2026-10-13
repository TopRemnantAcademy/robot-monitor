# G-7 实证：AgentVerse 能否驱动 mock_fleet 的 100 台 agent？

> Issue #114 / 订单 36。**结论先行**：AgentVerse **可以**加载/构造 100 台 agent 对象，
> 但**不能**用它产出本系统所需的遥测（telemetry）——它是 LLM 文本对话框架，
> `agent.step()` 返回的是 `Message`（自然语言），不是 `mock_fleet` 的数值 payload。
> 因此 **G-7 的「AgentVerse 驱动 mock_fleet」路线判定为不可行**；
> 既有 `simulator/task_sim.py`（Issue #63）已是本仓正确的任务级仿真形态。

---

## 0. 一句话事实

| 命题 | 实测结论 | 证据 |
| :--- | :--- | :--- |
| AgentVerse 能否**加载** 100 台 agent 定义 | ✅ 能（构造成对象，0.002s） | 探针 2 JSON |
| AgentVerse 能否**产出遥测** | ❌ 不能（Message 无任何遥测字段） | 探针 3 JSON |
| 无 API key 时能否 `import agentverse` | ❌ 被硬 gate（import 期即崩） | 探针 1 报错原文 |
| 即便给一个可用的 LLM，是否就能产遥测 | ❌ 仍然只有文本 Message | `verify_with_local_llm.sh` |

**根因**：AgentVerse 的 `BaseAgent` 模型强制字段 `llm: BaseLLM`（`agentverse/agents/base.py:21`），
每个 agent 的 `step()` 都是一次 LLM 推理；其输出语义槽是 `Action` / `Action Input`
（由任务 prompt 约定），与「`robot_id` + 24 关节温度/扭矩 + 电量 + 位姿」的数值契约不同层。

---

## 1. 物料现状（实证，非推测）

- 仓库 `external/` 下**实际不存在** AgentVerse 源码。`external/` 仅 4 个元数据文件
  （`SOURCES.md` / `INTEGRATION-PLAN.md` / `ORDER12-VERIFICATION.md` / `fetch-sources.sh`），
  且 `.gitignore` 明确 `external/*` 不入库（只留归因清单）。
- `docs/UPSTREAM.md:75` 记录 AgentVerse「`f90c4bd9` / 31 MB / 多机器人任务求解框架（LLM，备选）」，
  `STATUS.md:98` 记录「已下载（未验证能否驱动 100 agent）」。
- 本次实证**重新拉取**了该物料（`git clone --depth 1 https://github.com/OpenBMB/AgentVerse`），
  HEAD 实测 `f90c4bd`，**与 UPSTREAM 记录一致** → 记录属实，只是物料未随仓交付。

> 复现：`git clone --depth 1 https://github.com/OpenBMB/AgentVerse /tmp/agentverse_probe`
> （实测 38 MB，`git rev-parse --short HEAD` = `f90c4bd`）

## 2. AgentVerse 的依赖与架构（源码实证）

`requirements.txt`（原文）：

```
pyyaml
fastapi==0.95.1
uvicorn
py3langid
setuptools-scm
openai==1.1.0          ← LLM 客户端是必装依赖
opencv-python==4.8.0.76
gradio
httpx[socks]==0.25.0
astunparse
langchain==0.0.157
scikit-learn
pydantic==1.10.7       ← 钉在 pydantic v1（与本仓 pydantic v2 冲突）
...
```

`agentverse/agents/base.py:21`（原文）：

```python
class BaseAgent(BaseModel):
    name: str
    llm: BaseLLM            # ← 强制 LLM 字段，无「无 LLM 的 agent」路径
    output_parser: OutputParser
    ...
```

`agentverse/llms/openai.py:37-58`（原文节选）：

```python
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
...
if not OPENAI_API_KEY and not AZURE_API_KEY:
    logger.warn("OpenAI API key is not set. ...")
elif OPENAI_API_KEY:
    DEFAULT_CLIENT = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    ...
```

**关键**：`DEFAULT_CLIENT` **只在有 key 的分支里定义**。没有 key → 该符号不存在，
而 `agentverse/memory/chat_history.py:15` 会 `from agentverse.llms.openai import DEFAULT_CLIENT`
→ **import 期即 `ImportError`**。

## 3. 实测证据

### 3.1 探针脚本（可复现）

```bash
# 依赖（用 aliyun 镜像；清华镜像实测对 .whl 返回 403）
pip install -i https://mirrors.aliyun.com/pypi/simple/ \
    numpy "openai==1.1.0" "httpx==0.25.0" colorama colorlog \
    py3langid "tiktoken==0.5.1" rapidfuzz "langchain==0.0.157" scikit-learn astunparse

python simulator/agentverse_probe/verify_agentverse.py --av-root /tmp/agentverse_probe
```

### 3.2 实测输出（原始）

```
[探针 1] 无 OPENAI_API_KEY 时 import agentverse
  → 失败：ImportError: cannot import name 'DEFAULT_CLIENT' from
          'agentverse.llms.openai' (/tmp/agentverse_probe/agentverse/llms/openai.py)

[探针 2] 构造 100 个 agent（需 OPENAI_API_KEY 占位）
  → 构造 100 个 agent 对象成功，耗时 0.00s
  → 首个 agent: ConversationAgent, llm=OpenAIChat

[探针 3] agent 单步产物是否命中遥测契约
  → Message 字段: ['content', 'receiver', 'sender', 'sender_agent', 'tool_response']
  → 命中遥测契约字段: 无
  → 含遥测数值: False

结论
  1) 无 API key 时 import 直接被 gate（DEFAULT_CLIENT 未定义）: True
  2) 有 key 占位时可构造 agent 对象: 100 台
  3) agent 单步产物是否携带遥测数值: False
  ⇒ 判定：AgentVerse 可加载/构造 100 agent，但【不能】直接产出本系统遥测。
  verdict = loadable-but-not-telemetry
```

机器可读报告：`simulator/agentverse_probe/verify_agentverse_report.json`。

### 3.3 排除「只是没 key 所以失败」的质疑

给 AgentVerse 一个**可用的本地 LLM**（OpenAI 兼容假服务），看它是否就能产遥测：

```bash
bash simulator/agentverse_probe/verify_with_local_llm.sh
```

实测输出：

```
[单 agent] astep 返回 content='humanoid keepalive'
[100 agent] 环境单步返回 1 条 Message（文本对话）
[100 agent] 命中遥测契约字段: 无
[结论] LLM 可用时 AgentVerse 仍只产文本 Message；遥测需另一层从状态派生。
```

即：**LLM 可用也救不了契约**——AgentVerse 的产物是自然语言对话，不是机器人遥测。

### 3.4 对照：mock_fleet 的契约（`simulator/mock_fleet.py`）

`RobotState.build_payload()` 产出的 17 个字段：

```
robot_id, status_code, error_code, battery_pct, battery_temp,
max_joint_temp, joint_temperatures, joint_positions, joint_torques,
pos_x, pos_y, pos_z, orient_w, orient_x, orient_y, orient_z, ts
```

AgentVerse 的 `Message` 字段（`content/receiver/sender/sender_agent/tool_response`）
与之**交集为空**。

## 4. 结论与处置

1. **AgentVerse 不可用于「驱动 mock_fleet 产遥测」**：
   - 它是 LLM 多智能体**对话/任务求解**框架，不是机器人物理/遥测仿真器；
   - 要让它「驱动」100 台机器人，需自写一层 adapter，把 `task_load`/关节状态
     从 LLM 文本里反解——本质是**在 LLM 外面再套一层仿真**，LLM 本体没有增益；
   - 生态代价：钉 `pydantic==1.10.7`（本仓是 v2）、`openai==1.1.0`、`fastapi==0.95.1`，
     引入即与既有栈冲突；且每次 `step` 都烧 LLM token（100 agent × 10Hz 不可行——
     10Hz 遥测要求下，AgentVerse 的 LLM 往返延迟（秒级）数量级不符）。
2. **G-7 的「任务级多机器人协同仿真」需求已由本仓 `simulator/task_sim.py` 满足**
   （Issue #63 交付，已在 `main`）：
   - 任务调度器让遥测在时间上相关（流水线工位轮转 / 故障传播），相关性来自调度逻辑、
     非硬编码系数；
   - 复用 `mock_fleet` 的 payload 构造器与 MQTT 发布器，wire format 与既有管线一致。
   - 实测：pipeline 场景 12 台 r=−0.901（相邻工位反相关）、fault 场景 r=+0.991（中枢-邻居正相关），
     均 ≥0.3 阈值，退出码 0。
3. **处置**：AgentVerse 作为「任务级仿真驱动」**判不可行，不再引入**；
   `docs/UPSTREAM.md` 中该条从「备选」改为「已评估，未采纳」并注明理由。
   G-7 关闭时说明「AgentVerse 路线暂挂，任务级仿真由 `task_sim.py` 承担」。

## 5. 已知偏差 / 未验证项（诚实标注）

| 项 | 状态 |
| :--- | :--- |
| AgentVerse clone / 导入 / 构造 100 agent | ✅ 实测（本文所有报错均为原文） |
| 无 key 时 import 崩溃 | ✅ 实测（`ImportError: DEFAULT_CLIENT`） |
| LLM 可用时仅产文本 Message | ✅ 实测（本地假 LLM，`verify_with_local_llm.sh`） |
| AgentVerse **真实**调用 GPT-4 跑 100 agent 的吞吐 | ❌ **未验证**（无真实 OpenAI 额度；但这不影响契约层结论） |
| AgentVerse 的 `tasksolving` 框架能否产出结构化数值 | ❌ 未验证（其设计为文本角色协作，未做数值仿真，判断：与本需求不同层） |
