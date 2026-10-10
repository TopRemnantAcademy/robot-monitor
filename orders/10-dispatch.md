# 派工单：订单 10 — 告警抑制（inhibition）机制（I-5）

> 依据：`external/INTEGRATION-PLAN.md` 的 **I-5** 章节（对照 `prometheus/alertmanager` 官方文档）
> 触发方式：`post-issue-comment`（本文件**不含**触发词，避免重复触发构建）

---

## 0. 问题：同一台机器人会同时刷出多条告警

对照 Alertmanager（`external/alertmanager/docs/configuration.md`）时发现，
我们**实现了 `group_by` 与 `repeat_interval` 的等价物，但缺第三个概念：`inhibition`（抑制）**。

**具体表现**：机器人已判定 `offline`（severity 3，最严重）时，
`ticker` 线报离线，而 `consume` 线仍在跑 `motor_temp` / `battery_low` / `joint_stuck`
—— 于是同一台机器同时刷出 2 条以上告警，值班人员看到的是噪声而非信号。

> 逻辑上：机器人都离线了，它「电量低」「关节卡死」这类告警**没有处置价值**，
> 根因就是离线。这正是 Alertmanager 的 `inhibit_rules` 要解决的问题。

## 1. 现有代码坐标（主代理已读，可直接定位）

`alert_engine/app/main.py`：

| 行 | 内容 | 与本单关系 |
|---|---|---|
| 34 | `RULES = [MotorTempRule(), BatteryLowRule(), JointStuckRule(), OfflineRule()]` | 4 条规则 |
| 36 | `STREAM_RULES = [r for r in RULES if r.rule_id != "offline"]` | 消费线跑这 3 条 |
| 37 | `OFFLINE_RULE = ...` | ticker 线单独跑 |
| 41 | `online_state: dict[str, bool] = {}` | ⭐ **已有在线状态字典，可复用** |
| 53-58 | `async def emit(alert)` | ⭐ **抑制检查应加在这里** |
| 83-90 | 消费线：遍历 `STREAM_RULES` → `spawn(emit(alert))` | 被抑制的来源 |
| 111-114 | `online = ls is not None and (now - ls) <= offline_after_s`<br>`if online_state.get(rid) is not online:` | ⭐ ticker 已在维护该字典 |

**关键设计点**：`online_state` 由 ticker 每秒维护，且**已经是正确的在线判定**。
抑制逻辑可以直接复用它，**不需要额外维护一套 offline 告警生命周期**。

## 2. 任务

### T1 — 在 `emit()` 入口加抑制判定

语义（对齐 Alertmanager 的 `inhibit_rules`）：

```
source:  rule_id = "offline"        (severity 3，最高)
target:  同一 robot_id 的其他所有规则
equal:   [robot_id]
```

即：若 `online_state.get(robot_id) is False`，则该机器人的
`motor_temp` / `battery_low` / `joint_stuck` 告警**不落库、不发布**。

要求：
- 抑制判定**只作用于非 offline 规则**（offline 自身不能被自己抑制，否则永远报不出来）
- 被抑制的告警要 **DEBUG 级日志**说明（便于排查"为什么没告警"），不要静默丢弃
- `emit()` 的现有去重逻辑（`should_emit`）保持不变，抑制是**前置**于去重的一层

### T2 — 抑制的可配置化

在 `alert_engine/app/config.py` 增加开关，默认**开启**：

```python
inhibit_offline: bool = True   # 离线时抑制同机器人的其他告警
```

理由：抑制是行为变更，留一个关闭开关便于回滚与 A/B 对比。

### T3 — 边界处理（必须考虑）

| 场景 | 期望行为 |
|---|---|
| 机器人从离线恢复 | 抑制自动解除（`online_state` 变 True），无需额外清理 |
| `online_state` 尚无该 robot（刚启动） | **不抑制**（`dict.get()` 返回 None ≠ False），避免启动瞬间吞掉真实告警 |
| offline 规则自身 | **永不抑制** |
| 抑制期间积压的告警 | 不补发（抑制即丢弃，这是 Alertmanager 语义） |

> ⚠️ 第 2 条特别重要：必须用 `is False` 而不是 `not online_state.get(rid)`，
> 否则启动初期字典为空时会把**所有**告警都误抑制掉。

## 3. 验收标准

- [ ] `emit()` 对 `rule_id == "offline"` 永不抑制
- [ ] `online_state[rid] is False` 时，其他 3 条规则的告警被抑制且不落库
- [ ] 机器人恢复在线后抑制自动解除
- [ ] `online_state` 无记录时**不抑制**（用 `is False` 判定，非 falsy 判定）
- [ ] 配置项 `inhibit_offline` 存在且默认 True；设为 False 时行为同改动前
- [ ] 被抑制时有 DEBUG 日志
- [ ] **实际运行测试**（不是只看代码）：构造场景验证抑制与解除

## 4. 测试方法（本机可跑，不要再说跑不起来）

本机验证通道已打通（主代理实测）：

```bash
python -m venv .venv && .venv/Scripts/python.exe -m pip install \
  -i https://pypi.tuna.tsinghua.edu.cn/simple pytest pytest-asyncio redis pydantic-settings

cd alert_engine && PYTHONPATH=. python -m pytest tests/ -v
```

> 若 `redis` / `asyncpg` 装不上，用镜像源（上面已带 `-i`）。
> 规则逻辑是纯 Python，可用 stub 替掉 `pydantic_settings` 后直接测 ——
> 主代理此前就是用这个方法实测规则逻辑 20/20 通过的。

至少覆盖：
1. offline 激活时，battery_low 被抑制
2. offline 自身不被抑制
3. 恢复在线后，battery_low 重新发出
4. `online_state` 为空时不被抑制
5. `inhibit_offline=False` 时行为不变

## 5. 禁止项

1. **禁止**改 4 条规则自身的判定逻辑（本单只加抑制层）
2. **禁止**用 falsy 判定 `not online_state.get(rid)` —— 必须 `is False`
3. **禁止**修改 `deduplicator.py`
4. **禁止**伪造测试结果；跑不出来写「未验证 + 所需条件」
5. **禁止**只推分支就宣称完成 —— 尝试 push + 开 PR；因 `repo-code:rw` 失败则
   **原文报告错误**
6. **禁止**删除或修改 `external/INTEGRATION-PLAN.md` 的审计结论

## 6. 交付方式

1. 分支 `feat/order-10-alert-inhibition`（基于最新 `main`）
2. 提交信息写明抑制语义与测试结果
3. 尝试 push + 开 PR 到 `main`
4. push 失败则把完整 diff 与测试输出写进本 issue 评论

---

**定位：这是对照 Alertmanager 补齐缺失的第三个告警概念。核心陷阱是 `is False` vs falsy —— 写错就会在启动瞬间吞掉所有告警。**
