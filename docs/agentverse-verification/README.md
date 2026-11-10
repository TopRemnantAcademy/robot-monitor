# G-7 Evidence: Can AgentVerse drive mock_fleet's 100 agents?

> Issue #114 / order 36. **Conclusion first**: AgentVerse **can** load/construct 100 agent objects,
> but **cannot** use them to produce the telemetry this system needs — it is an LLM text-dialogue
> framework; `agent.step()` returns a `Message` (natural language), not `mock_fleet`'s numeric payload.
> Therefore the G-7 route "AgentVerse drives mock_fleet" is judged infeasible; the existing
> `simulator/task_sim.py` (Issue #63) is already this repo's correct task-level simulation form.

---

## 0. The bottom line

| Claim | Result | Evidence |
| :--- | :--- | :--- |
| Can AgentVerse **load** 100 agent definitions | ✅ Yes (constructed as objects, 0.002s) | Probe 2 JSON |
| Can AgentVerse **produce telemetry** | ❌ No (Message has no telemetry fields) | Probe 3 JSON |
| Can `import agentverse` without an API key | ❌ Hard-gated (crashes at import time) | Probe 1 original error |
| Would a usable LLM make it produce telemetry | ❌ Still only a text Message | `verify_with_local_llm.sh` |

**Root cause**: AgentVerse's `BaseAgent` model forces the field `llm: BaseLLM`
(`agentverse/agents/base.py:21`); each agent's `step()` is one LLM inference; its output semantic
slots are `Action` / `Action Input` (defined by the task prompt), which are a different layer from
the numeric contract of "`robot_id` + 24 joint temps/torques + battery + pose".

---

## 1. Current materials (verified, not speculative)

- The AgentVerse source **does not actually exist** under `external/`. `external/` holds only 4
  metadata files (`SOURCES.md` / `INTEGRATION-PLAN.md` / `ORDER12-VERIFICATION.md` / `fetch-sources.sh`),
  and `.gitignore` explicitly keeps `external/*` out of the repo (only the attribution list remains).
- `docs/UPSTREAM.md:75` records AgentVerse as "`f90c4bd9` / 31 MB / multi-robot task-solving
  framework (LLM, fallback)", and `STATUS.md:98` records "downloaded (unverified whether it can
  drive 100 agents)".
- This verification **re-pulled** the material (`git clone --depth 1 https://github.com/OpenBMB/AgentVerse`),
  with HEAD measured at `f90c4bd`, **consistent with the UPSTREAM record** → the record is accurate,
  only the material was not delivered with the repo.

> Reproduce: `git clone --depth 1 https://github.com/OpenBMB/AgentVerse /tmp/agentverse_probe`
> (measured 38 MB, `git rev-parse --short HEAD` = `f90c4bd`)

## 2. AgentVerse's dependencies and architecture (source-evidenced)

`requirements.txt` (original):

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

`agentverse/agents/base.py:21` (original):

```python
class BaseAgent(BaseModel):
    name: str
    llm: BaseLLM            # ← 强制 LLM 字段，无「无 LLM 的 agent」路径
    output_parser: OutputParser
    ...
```

`agentverse/llms/openai.py:37-58` (original excerpt):

```python
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
...
if not OPENAI_API_KEY and not AZURE_API_KEY:
    logger.warn("OpenAI API key is not set. ...")
elif OPENAI_API_KEY:
    DEFAULT_CLIENT = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    ...
```

**Key**: `DEFAULT_CLIENT` is **only defined in the branch that has a key**. No key → the symbol
doesn't exist, and `agentverse/memory/chat_history.py:15` does
`from agentverse.llms.openai import DEFAULT_CLIENT` → **`ImportError` at import time**.

## 3. Empirical evidence

### 3.1 Probe script (reproducible)

```bash
# 依赖（用 aliyun 镜像；清华镜像实测对 .whl 返回 403）
pip install -i https://mirrors.aliyun.com/pypi/simple/ \
    numpy "openai==1.1.0" "httpx==0.25.0" colorama colorlog \
    py3langid "tiktoken==0.5.1" rapidfuzz "langchain==0.0.157" scikit-learn astunparse

python simulator/agentverse_probe/verify_agentverse.py --av-root /tmp/agentverse_probe
```

### 3.2 Raw measured output

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

Machine-readable report: `simulator/agentverse_probe/verify_agentverse_report.json`.

### 3.3 Ruling out the "it only failed because there's no key" objection

Give AgentVerse a **usable local LLM** (an OpenAI-compatible fake service) and see whether it then
produces telemetry:

```bash
bash simulator/agentverse_probe/verify_with_local_llm.sh
```

Measured output:

```
[单 agent] astep 返回 content='humanoid keepalive'
[100 agent] 环境单步返回 1 条 Message（文本对话）
[100 agent] 命中遥测契约字段: 无
[结论] LLM 可用时 AgentVerse 仍只产文本 Message；遥测需另一层从状态派生。
```

That is: **a usable LLM still can't save the contract** — AgentVerse's output is natural-language
dialogue, not robot telemetry.

### 3.4 Contrast: mock_fleet's contract (`simulator/mock_fleet.py`)

`RobotState.build_payload()` produces 17 fields:

```
robot_id, status_code, error_code, battery_pct, battery_temp,
max_joint_temp, joint_temperatures, joint_positions, joint_torques,
pos_x, pos_y, pos_z, orient_w, orient_x, orient_y, orient_z, ts
```

The intersection between AgentVerse's `Message` fields
(`content/receiver/sender/sender_agent/tool_response`) and these is **empty**.

## 4. Conclusion and disposition

1. **AgentVerse cannot be used to "drive mock_fleet to produce telemetry"**:
   - It is an LLM multi-agent **dialogue/task-solving** framework, not a robot physics/telemetry
     simulator;
   - To make it "drive" 100 robots you would have to write an adapter that reverse-derives
     `task_load`/joint state from LLM text — essentially **wrapping another simulation layer
     outside the LLM**, with no gain from the LLM itself;
   - Ecosystem cost: it pins `pydantic==1.10.7` (this repo is v2), `openai==1.1.0`,
     `fastapi==0.95.1`, introducing conflicts with the existing stack; and every `step` burns LLM
     tokens (100 agents × 10Hz is infeasible — under the 10Hz telemetry requirement, AgentVerse's
     LLM round-trip latency (seconds) is orders of magnitude off).
2. **G-7's "task-level multi-robot collaborative simulation" need is already met by this repo's
   `simulator/task_sim.py`** (Issue #63 deliverable, already on `main`):
   - The task scheduler makes telemetry temporally correlated (pipeline station rotation / fault
     propagation); the correlation comes from scheduling logic, not hard-coded coefficients;
   - It reuses `mock_fleet`'s payload builder and MQTT publisher, so the wire format matches the
     existing pipeline.
   - Measured: pipeline scenario 12 robots r=−0.901 (adjacent stations anti-correlated), fault
     scenario r=+0.991 (hub-neighbor positively correlated), both ≥0.3 threshold, exit code 0.
3. **Disposition**: AgentVerse as a "task-level simulation driver" is judged infeasible and will
   **not be adopted**; in `docs/UPSTREAM.md` that entry changes from "fallback" to "evaluated, not
   adopted" with the reason noted. When G-7 closes, note "AgentVerse route shelved for now; task-level
   simulation handled by `task_sim.py`".

## 5. Known deviations / unverified items (honest disclosure)

| Item | Status |
| :--- | :--- |
| AgentVerse clone / import / construct 100 agents | ✅ Verified (all errors in this doc are original text) |
| import crashes without key | ✅ Verified (`ImportError: DEFAULT_CLIENT`) |
| Only produces text Message when LLM available | ✅ Verified (local fake LLM, `verify_with_local_llm.sh`) |
| AgentVerse **actually** calling GPT-4 to run 100 agents' throughput | ❌ **Unverified** (no real OpenAI quota; but this does not affect the contract-level conclusion) |
| Whether AgentVerse's `tasksolving` framework can produce structured numeric values | ❌ Unverified (its design is text-role collaboration, no numeric simulation; judgment: a different layer from this requirement) |
