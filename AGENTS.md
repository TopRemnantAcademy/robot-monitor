# AGENTS.md — RobotMonitor 全局工程纪律（注入每个 NPC 系统提示词首段）

本仓库是一个 **100 台人形机器人工厂监控系统**（2D 运维看板）。你在本仓库工作时的**绝对铁律**：

## 1. 无幻觉（最高优先级）
- 只使用仓库锁定文件（`requirements.txt` / `package.json`）中固定的依赖版本与 API；**禁止编造**不存在的库名、类名、函数名、API 端点、端口。
- 任何引用第三方库/API 的断言，必须能在 `requirements.txt` / `package.json` 或官方文档中找到。找不到就标注「未验证」，绝不假装存在。
- 禁止用 `...` / `// TODO` / `pass`（合理空实现除外）占位。生成文件必须完整可运行。

## 2. 实证优先（file:line）
- 提交代码（PR）时，描述必须包含关键实现点的 `路径:行号` 实证引用。
- 声称「已完成 / 已通过」前，必须实际运行验证（模拟器 / 类型检查 / 单元测试），并附命令与输出摘要。
- 无法在本地验证的项，单列「未验证」章节，说明还需什么才能验证。

## 3. 性能护栏（防 1000Hz 冲垮）
- 后端：**严禁逐条 INSERT**；必须用 `asyncio.Queue` 批量（满 500 条或满 1 秒双重触发）写入 TimescaleDB。
- 前端：**严禁每条 WS 消息触发 React 重渲染**；必须 100ms 节流（useRef 缓冲 + setInterval 批量刷 store）。
- 任何吞吐相关改动都要先算峰值（100 台 × 10Hz = 1000 msg/s）再动手。

## 4. 数据契约
- 机器人 payload 字段以 `backend_ingestion/app/schemas/telemetry.py` 与 `simulator/mock_fleet.py` 中定义的遥测 Schema 为准（`robot_id` 前缀 `humanoid-`，含 `max_joint_temp` 预计算列）。
- 数据库 Schema 以 `infra/init.sql` 为准，改动 Schema 必须同步更新 init.sql 与对应 ORM 模型。

## 5. 安全
- **严禁把 `.env` / 密钥 / 令牌烘焙进 Docker 镜像或提交到仓库**；仓库根必须有 `.dockerignore` 排除 `.env`。
- 不在代码里硬编码生产凭据。

## 6. 工作流
- 模块构建严格按以下**六个阶段**顺序推进，不要跳步
  （基础设施 → 模拟器 → 后端 → 告警引擎 → 前端 → 联调；模拟器必须先于后端产出，否则后端无法验收）。
- 每个模块完成后，必须用 `simulator/mock_fleet.py` 跑通端到端链路。
- 区分「事实」与「判断」，判断需注明依据。

## 7. 提交与协作
- 不要直接 force-push main；通过 PR 提交，等待审查角色（审查工程师）通过。
- 中文沟通，技术标识符（文件/类名/端点）保留英文原文。
