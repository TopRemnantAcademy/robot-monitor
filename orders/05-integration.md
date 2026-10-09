# [05] 端到端联调集成测试

## 目标
编写 `test_integration.sh`（仓库根已存在脚手架，需完善），自动化完成：起中间件 → 后端 → 告警引擎 → 模拟器压测 30s → 校验 TimescaleDB 写入与告警触发。

## 依据规范
- `spec/05_master_execution_playbook.md`（阶段四）
- `spec/ci/integration-call.md`（集成巡检程序）
- `infra/docker-compose.yml` / `infra/init.sql`

## 硬性要求（交给开发 / 测试 NPC 的指令）
1. 完善仓库根 `test_integration.sh`，使其按顺序执行：
   a. `docker compose -f infra/docker-compose.yml up -d`（EMQX + TimescaleDB + Redis）
   b. 等待三组件健康（`pg_isready` / `redis-cli ping`）
   c. 启动 FastAPI 后端（`uvicorn app.main:app --port 8000`），等 `/healthz`=200
   d. 启动 `alert_engine`
   e. 运行 `python simulator/mock_fleet.py --robots 100` 压测 30s
   f. 查询 TimescaleDB 校验 `robot_telemetry` 写入总条数接近 30,000
   g. 查询 `robot_alert` 验证过热 / 低电量告警被触发
2. 输出结构化集成报告（参考 `spec/ci/integration-call.md` 格式）。

## 验收标准
- [ ] 脚本可重复运行（含健康检查与超时保护）
- [ ] 校验使用实际 SQL 计数（不以「日志无报错」代替通过）
- [ ] 报告格式含：基础设施状态 / 后端 / 压测 / telemetry 行数 / alert 行数 / 结论
- [ ] 区分环境错误 vs 代码错误（docker 不可用时标「未验证」）
- [ ] 通过 PR 提交
