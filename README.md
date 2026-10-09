# RobotMonitor — 100 台人形机器人工厂监控系统

监控 100 台人形机器人的 2D 运维看板：在线状态、电量、电机/关节温度、扭矩、故障码。
高频遥测（100 台 × 10Hz = 1000 msg/s）经 MQTT → FastAPI 批量写入 TimescaleDB，
Redis 缓存最新状态并 Pub/Sub，React 看板经 WebSocket 实时渲染。

## 架构

```
机器人(Edge) --MQTT--> [backend_ingestion] --batch--> TimescaleDB
                         |-- Redis(Hash+TTL) + Pub/Sub
[alert_engine] --订阅 stream--> 规则判定 --> robot_alert + 前端告警流
[frontend_dashboard] <--WebSocket /ws/robots-- [backend_ingestion]
[simulator] --MQTT--> 模拟 100 台 @10Hz + 异常注入（压测用）
```

## 目录

| 路径 | 说明 |
| :--- | :--- |
| `spec/` | 6 份规范（00 架构 / 01 后端 / 02 告警 / 03 前端 / 04 模拟器 / 05 剧本），NPC 的输入 |
| `infra/` | `docker-compose.yml`（EMQX+TimescaleDB+Redis）、`init.sql`（超表+连续聚合） |
| `backend_ingestion/` | FastAPI 采集存储（NPC 构建） |
| `alert_engine/` | 告警健康引擎（NPC 构建） |
| `frontend_dashboard/` | React 看板（NPC 构建） |
| `simulator/` | 100 台模拟器（NPC 构建） |
| `test_integration.sh` | 端到端联调脚本 |
| `.cnb.yml` / `.cnb/settings.yml` / `AGENTS.md` | CNB NPC 配置与全局纪律 |

## 本地快速开始

```bash
docker compose -f infra/docker-compose.yml up -d   # 起中间件
bash test_integration.sh                            # 端到端联调（需先构建各模块）
```

## NPC 工作流（CNB）

- 规范见 `spec/05_master_execution_playbook.md` 四阶段：基础设施 → 后端 → 告警/前端 → 联调。
- 派工：在 Issue 评论 `@flow.237/RobotMonitor(开发工程师) 请按本 Issue 实现`。
- 自主流水线：crontab `0 0,6,12,18 * * *` 跑 7 段（PM→架构→开发→审查→测试→合规→集成），读 `spec/ci/*-call.md`。
- 全局纪律见 `AGENTS.md`（无幻觉 / 完整可运行 / 性能护栏 / 数据契约 / 安全）。
