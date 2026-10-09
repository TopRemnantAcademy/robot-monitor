# 订单 01 — 基础设施（已完成骨架，本单为校验/补齐）

> 派工：`@flow.237/RobotMonitor(时序数据库专家)`
> 顺序：**第 1 单**（其余全部依赖本单）
> 依据：`spec/00` §3 / §4 / §5.5、`spec/05` 阶段一

## 状态
主代理已产出 `infra/docker-compose.yml` 与 `infra/init.sql`。本单为**校验 + 补齐**，不要推倒重写。

## 必须校验的硬指标（逐项给 file:line 实证）
1. `robot_telemetry` 有 `PRIMARY KEY (time, robot_id)`（**必须含分区列 time**，否则 TimescaleDB 拒绝）。
2. `robot_telemetry` / `robot_alert` 均已 `create_hypertable`（chunk 7 天，带 `if_not_exists`）。
3. `robot_status_10s` 连续聚合存在，且 `add_continuous_aggregate_policy` 已配置。
   - **注意**：已核实 TimescaleDB 2.7+ 连续聚合支持 `first()`/`last()`，本项目锁 2.13.1，
     `last(status_code, time)` **是合法的，不要"修正"成 max()**。
4. `robot_metadata` 已**预置 100 台**（`humanoid-001` ~ `humanoid-100`）。
5. 索引：`(robot_id, time DESC)` 于 telemetry 与 alert 两表。
6. compose 三组件 EMQX / TimescaleDB / Redis 均有 healthcheck。

## 补齐项（若缺失则补）
- compose 增加 `restart: unless-stopped`（监控栈应自愈）。
- 若 `robot_metadata` 预置缺失，补 `generate_series(1,100)` 播种语句。

## 验收标准
- [ ] `docker compose -f infra/docker-compose.yml up -d` 成功
- [ ] `SELECT count(*) FROM robot_metadata;` = **100**
- [ ] `SELECT count(*) FROM robot_telemetry;` 可执行（表已建）
- [ ] 三条 healthcheck 均通过（EMQX / TimescaleDB / Redis）
- [ ] PR 描述含校验点的 `路径:行号`

## 禁止
- 禁止删除 `last()` 连续聚合（已核实合法）。
- 禁止改动 `PRIMARY KEY (time, robot_id)` 为不含 time 的形式。
- 禁止提交 `.env` 或任何密钥。
