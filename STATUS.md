# STATUS — RobotMonitor 构建任务看板

> PM 角色定时维护本文件（见 `spec/ci/pm-call.md`）。订单详情见 `orders/0X-*.md`。

| # | 模块 | 规范 | 订单文件 | 状态 | 派工 Issue |
| - | :--- | :--- | :--- | :--- | :--- |
| 01 | 后端采集存储 | spec/01 | orders/01-backend.md | 待实现 | GitHub #1 |
| 02 | 告警健康引擎 | spec/02 | orders/02-alert-engine.md | 待实现 | GitHub #2 |
| 03 | 前端看板 | spec/03 | orders/03-frontend.md | 待实现 | GitHub #3 |
| 04 | 模拟器压测 | spec/04 | orders/04-simulator.md | 待实现 | GitHub #4 |
| 05 | 联调集成 | spec/05 | orders/05-integration.md | 待实现 | GitHub #5 |

## 当前阻塞
- **CNB 仓库未创建**：CNB 令牌缺 `group-resource:rw`，`create-repo` 403。需用户网页建仓（cnb.cool/flow.237 → 新建 RobotMonitor）或重授权令牌。
- 解封后：推 CNB → `build-crontab-sync` → 将 `orders/0X-*.md` 作为 CNB Issue 发布并 `@flow.237/RobotMonitor(开发工程师)` 派工。

## 已完成地基
- spec/00~05 规范、infra（compose+init.sql）、CNB NPC 三件套（.cnb/.yml / .cnb/settings.yml / AGENTS.md）、spec/ci/*-call.md、test_integration.sh。
- 已推 GitHub（commit 327bcb2），待推 CNB。
