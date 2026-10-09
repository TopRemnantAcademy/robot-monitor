# 订单 06 — 端到端联调与压测（收口）

> 派工：`@flow.237/RobotMonitor(测试工程师)` 与 `@flow.237/RobotMonitor(压测与性能工程师)`
> 顺序：**第 6 单**（最后，依赖 01~05 全部完成）
> 依据：`spec/05` 阶段六、`spec/ci/integration-call.md`、`spec/04`

## 背景
主代理已产出 `test_integration.sh` 骨架，并修复了三处缺陷：
- 补齐 EMQX 就绪等待（否则连不上 Broker → 写入 0 条 → 假失败）
- PID 变量先初始化（旧版在 `set -u` 下若 alert_engine 未实现会直接中止）
- 后端改为等待 `/healthz` 200，而非固定 sleep 5

## 任务
1. 完善 `test_integration.sh`，按序执行：
   1. `docker compose -f infra/docker-compose.yml up -d`
   2. 等待 **三个组件**（TimescaleDB / Redis / **EMQX**）就绪
   3. 校验 `robot_metadata` = 100
   4. 启动后端，等待 `/healthz` = 200
   5. 启动 alert_engine
   6. 跑 `python simulator/mock_fleet.py --robots 100` 压测 30s
   7. 校验 `robot_telemetry` ≈ **30,000** 条
   8. 校验 `robot_alert` 触发过热 / 低电量
2. 输出结构化集成报告（参照 `spec/ci/integration-call.md` 格式）。
3. 压测工程师额外产出性能报告：实测 TPS、队列积压水位、批量落库耗时、前端节流是否生效。

## 验收标准
- [ ] 脚本**可重复运行**，含健康检查与超时保护
- [ ] 校验使用**真实 SQL 计数**，禁止用"日志无报错"代替通过
- [ ] 报告含：基础设施状态 / 后端 / 压测 / telemetry 行数 / alert 行数 / 结论
- [ ] 区分「环境错误」与「代码错误」，两栏分开列
- [ ] 未通过的项标「未验证」并说明所需条件
- [ ] 性能报告区分**实测值**与**推测**，瓶颈定位给 `路径:行号`

## 上报纪律
- 不得因某一模块未实现就整体判失败——未实现的模块标「跳过（阶段 X 未完成）」。
- 不得臆断通过；所有"通过"必须附实际命令与输出摘要。
