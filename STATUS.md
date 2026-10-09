# STATUS — RobotMonitor 构建任务看板

> PM 角色定时维护本文件（见 `spec/ci/pm-call.md`）。订单详情见 `orders/0X-*.md`。

## 一、执行顺序（依赖正确，不可跳步）

```
01 基础设施 ──> 02 模拟器 ──> 03 后端 ──> 04 告警引擎 ──> 05 前端 ──> 06 联调
 (compose+init)  (数据源先行)   (用02验收)  (消费03的Redis流) (用03的WS+REST)  (收口)
```

> 修订：旧版把模拟器排最后，却要求后端阶段「用模拟器跑 30,000 条」——依赖倒置。
> 模拟器只依赖 MQTT Broker，必须先产出。

| # | 模块 | 规范 | 订单 | 派工角色 | 状态 |
| - | :--- | :--- | :--- | :--- | :--- |
| 01 | 基础设施 | spec/00 §3/§4 | orders/01-infra.md | 时序数据库专家 | 骨架已完成，待校验 |
| 02 | 模拟器 | spec/04 | orders/02-simulator.md | 后端接入工程师 | 待实现 |
| 03 | 后端采集存储 | spec/01 | orders/03-backend.md | 后端接入工程师 | 待实现 |
| 04 | 告警引擎 | spec/02 | orders/04-alert-engine.md | 告警引擎工程师 | 待实现 |
| 05 | 前端看板 | spec/03 | orders/05-frontend.md | 前端看板工程师 | 待实现 |
| 06 | 联调压测 | spec/05 阶段六 | orders/06-integration.md | 测试 + 压测 | 待实现 |

## 二、开工前规划审计（已完成，10 项缺陷已修）

| # | 缺陷 | 位置 | 处理 |
| - | :--- | :--- | :--- |
| A | Drawer 要 5 分钟历史但 REST 端点缺失 | spec/03:88 vs spec/01 | ✅ spec/00 §5.3 + spec/01 §4.5 增 `/robots/{id}/history` |
| B | WS 消息无 `type`，alert 无法送达前端 | spec/00:119 | ✅ §5.2 统一信封（snapshot/telemetry/alert） |
| C | ORM 声明复合 PK 但 DB 无 PK | spec/01:62 vs init.sql | ✅ 增加 `PRIMARY KEY (time, robot_id)` |
| D | 2 阶段要 simulator 验收但 4 阶段才生成 | spec/05:25 vs :39 | ✅ 重排为 6 阶段（模拟器第 2） |
| E | `set -u` 下 `ALERT_PID` 未绑定即崩溃 | test_integration.sh | ✅ 变量预先初始化 |
| F | 无 EMQX 就绪等待 → 写入 0 条假失败 | test_integration.sh | ✅ 三组件全部等待 |
| G | `robot_metadata` 未播种 | init.sql | ✅ 100 台 generate_series 播种 |
| H | 前端要 `VITE_WS_HOST` 但 env 是 `WS_HOST` | spec/03:123 | ✅ 补 `VITE_WS_HOST` / `VITE_API_BASE` |
| I | offline 判定依赖 Redis TTL 过期事件 | spec/02:49 | ✅ 改为 ticker + 内存 last_seen（§3.1） |
| J | 关节命名无唯一事实来源 | spec/00:11 | ✅ §5.5 确定 24 个标准键 |

**核实过但并非缺陷（勿改）**：连续聚合中的 `last()` —— TimescaleDB 2.7+ 官方文档明确支持
CAgg 内 `first()/last()`，本项目锁 2.13.1，合法。

## 三、NPC 配置（10 角色）

`.cnb/settings.yml`：产品经理 / 架构师 / 时序数据库专家 / 后端接入工程师 / 告警引擎工程师 /
前端看板工程师 / 代码审查 / 测试工程师 / 压测与性能工程师 / 合规

`.cnb/.yml`：① Issue·PR 评论 `@flow.237/RobotMonitor(角色)` 派工 ② crontab `0 0,6,12,18 * * *` 7 段自主流水线

## 四、当前状态
- CNB 仓库 `flow.237/RobotMonitor` 已确认存在。
- 平台开关：`auto_trigger_by_npc: true`、`cron_auto_trigger: true`（NPC 与 cron 均已开启）。
