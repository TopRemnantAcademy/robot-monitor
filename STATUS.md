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
| 01 | 基础设施 | spec/00 §3/§4 | orders/01-infra.md | 时序数据库专家 | **已派工** (CNB Issue #1, build cnb-vvh-1k4guji0r) |
| 02 | 模拟器 | spec/04 | orders/02-simulator.md | — | **已完成**（NPC 产出但推送失败丢失；主代理本地实现并实测通过，11/11） |
| 03 | 后端采集存储 | spec/01 | orders/03-backend.md | — | **已实现（主代理）**，静态校验 13/13 通过；运行时未验（见 §七） |
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
- CNB 仓库 `flow.237/RobotMonitor` 已确认存在，代码已推送（main @ `5c0eab2`）。
- 平台开关：`auto_trigger_by_npc: true`、`cron_auto_trigger: true`（NPC 与 cron 均已开启）。
- crontab 已显式注册（`cnb build build-crontab-sync --repo flow.237/RobotMonitor --branch main` → 同步成功）。
- **已派工 01/02/03**，NPC 构建均为 `event: issue.comment@npc`，状态 pending。
- 待派工：04（告警引擎）、05（前端）、06（联调）—— 依序在 03 完成后派发。

## 五、当前阻塞（需用户处理，非代码问题）

| # | 阻塞 | 证据 | 需谁处理 |
| - | :--- | :--- | :--- |
| B1 | **AI 积分已用尽** | `[llm error] HTTP 402 [EXCEEDING_LIMIT] AI 积分已用尽，请联系根组织管理员提升配额` | 根组织管理员（提升配额） |
| B2 | **NPC 无推送权限** | `remote: Your token does not have the repo-code:rw scope required to push` → 403 | 授予 `repo-code:rw` / 开启 NPC 工作模式 |
| B3 | 派工时未带 `--work-mode` | 构建环境 `CNB_NPC_ENABLE_WORKMODE=false`、`workMode: false` | 已定位；下次派工补 `--work-mode` |

**后果**：NPC 实际写出了可用代码（build 02 实测 1020 msg/s、82°C+ 过热注入生效，本地 commit `97e0e96`），
但 push 403 失败 + 随后 402 断粮，CI 容器为临时环境 → **产出丢失**。

**注**：主代理凭据**有**推送权限（Bearer 头方式已验证可用），故 02 已由主代理本地落地。

## 六、环境要点（排障记录）
- **git push 到 CNB 必须用 Bearer 头**：
  `git -c http.extraHeader="Authorization: Bearer ${TOKEN}" push https://cnb.cool/flow.237/RobotMonitor.git main`
  （URL 内嵌 token、`cnb git-credential` helper 均会挂起；helper 对 `cnb.cool` 返回 `unknown host`。）
- **CI 配置必须在仓库根 `.cnb.yml`**，放 `.cnb/.yml` 时 `build-crontab-sync` 报 `CONFIG_YAML_EMPTY`。

## 七、验证边界（**未验证**事项，不得当作已通过）

| 模块 | 已验证 | 未验证 |
| :--- | :--- | :--- |
| 02 模拟器 | ✅ 11/11 实测（关节键名、payload 契约、过热 82°C+、电量 0.05%/s、status_code） | MQTT 真实收发（本机无 broker） |
| 03 后端 | ✅ 静态校验 13/13（跨模块关节键名一致、INSERT 列/占位符/元组三者=17 对齐、WS 三种 type、alert 转发、REST 路由、无占位符）；`py_compile` 全通过 | **运行时未验**：连库、批量落库吞吐、WS 端到端 |

**为何 03 无法运行时验证**：本机 PyPI 被网络策略拦截（`403 Forbidden`），依赖装不上；
且 `asyncpg==0.29.0` 在 Python 3.13 下源码编译失败（`_PyLong_AsByteArray` 签名变更）。
→ **这不构成缺陷**：部署镜像锁定 `python:3.11-slim`，该版本有 asyncpg 预编译 wheel。
真正的端到端验证需在有 Docker + 可连 PyPI 的环境执行 `bash test_integration.sh`。
