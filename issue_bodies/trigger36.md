@codebuddy 请接单执行 G-7（Issue #114：多机器人任务级仿真验证，AgentVerse 驱动 mock_fleet）。

完整规格已写在 Issue 正文（以及本地 `orders/36-dispatch.md`）。请严格遵循：

1. **验证优先，禁止强行造**。第一动作是查清 `simulator/mock_fleet.py` 与 `external/` 下 AgentVerse 物料现状；实测 AgentVerse 能否加载 100 台 agent 定义并跑任务级仿真，给出**日志/报错实证**，不得臆断"能/不能"。
2. **可行**则扩展 `simulator/`（新脚本或 `mock_fleet` 模式）产出多机器人任务级仿真 harness，让 ≥10 台 agent 协同产出遥测，且 `test_integration.sh` 或等价脚本可跑通。
3. **不可行**则原文贴证据（报错/资源限制），关单写明"G-7 暂挂"，**不要伪造 harness 或把波形伪装成任务级协同**。
4. **白名单**：`simulator/**`、`external/`（只读验证，不提交大物料）、`tests/**`、`docs/**`、`orders/36-dispatch.md`。白名单外 diff 即无效交付。
5. **禁止**改 `backend_ingestion/`、`alert_engine/`、`frontend_dashboard/` 主业；既有测试断言零改动、零弱化。
6. 交付分支 `feat/g7-agentverse-sim` → PR；PR 描述含 AgentVerse 验证实证 +（可行时）harness 实测输出 +（不可行时）证据结论。失败就原文贴错，禁止静默失败。

请开始。