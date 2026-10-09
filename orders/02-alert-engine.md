# [02] 告警与健康诊断引擎

## 目标
实现 `alert_engine/` 模块：消费后端 Redis `robot:stream:all` 实时状态，基于滑动窗口判定电机过热 / 低电量 / 离线 / 关节卡死，去重后写入 TimescaleDB `robot_alert` 表并 `PUBLISH robot:alerts`。

## 依据规范
- `spec/00_system_architecture_spec.md`（告警表 Schema、Redis key 约定）
- `spec/02_alerting_and_health_analytics_engine.md`（规则 / 去重 / 分发）

## 硬性要求（交给开发 NPC 的指令）
1. 四个规则类（`motor_temp` / `battery` / `offline` / `joint_stuck`）必须继承 `rules/base.py` 的 `Rule` 抽象基类，实现 `evaluate(state, window) -> Alert | None`。
2. `deduplicator.py` 必须用 Redis Key `alert:dedup:{robot_id}:{rule_id}` + `EX` = 去重窗口实现按 (robot_id, rule_id) 去重，禁止重复写库。
3. `main.py` 必须订阅 `robot:stream:all`，维护每台机器人滑动窗口（`collections.deque`），驱动规则判定循环。
4. `store.py` 必须把告警写入 TimescaleDB `robot_alert` 超表，并 `PUBLISH robot:alerts`。
5. 阈值与窗口全部从 `config.py` 读取。

## 判定规则（来自 spec/02）
| 规则 | 触发 | severity | 去重窗口 |
| :-- | :-- | :-- | :-- |
| motor_temp | 任一关节 > 75°C（持续 ≥3 采样） | 3 critical | 60s |
| battery_low | battery_pct < 15% | 2 warning | 120s |
| offline | last_seen > 10s | 3 critical | 30s |
| joint_stuck | 同关节 angle 方差≈0 但 torque>阈值（5s） | 2 warning | 60s |

## 验收标准
- [ ] 4 个规则类继承自 Rule 基类
- [ ] 去重器幂等（重复消息不重复写库）
- [ ] 落库 `robot_alert` 超表 + 发布 `robot:alerts`
- [ ] 跑模拟器时触发过热 / 低电量告警
- [ ] 无占位符；提供 `requirements.txt` 与 `README.md`
- [ ] 通过 PR 提交，含 `路径:行号` 实证
