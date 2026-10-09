# R-08 验收测试矩阵（逐用例编号 · 可判定）

> 每个用例必须给出**真实实测值**，禁止以「日志无报错」代替通过。
> 环境不具备的用例标 **未验证** 并写明所需条件，不得臆断通过。

## 0. 通用规则

- 判定只有三种：`PASS` / `FAIL` / `未验证`
- 每次判定必须附：命令 + 实际输出摘要
- 区分**环境问题**（docker/网络/端口）与**代码问题**，分开列

## 1. 基础设施（订单 01）

| # | 用例 | 步骤 | 期望 | 判定依据 |
| - | :--- | :--- | :--- | :--- |
| A1 | 三组件启动 | `docker compose -f infra/docker-compose.yml up -d` | 三容器 healthy | `docker ps` |
| A2 | 元数据播种 | `SELECT count(*) FROM robot_metadata;` | **= 100** | SQL 实际计数 |
| A3 | 超表建立 | 查 `robot_telemetry` / `robot_alert` | 均为 hypertable | SQL |
| A4 | 连续聚合 | 查 `robot_status_10s` | 视图存在 | SQL |
| A5 | 复合主键 | 查 `robot_telemetry` 约束 | PK 含 `(time, robot_id)` | SQL |

## 2. 模拟器（订单 02）

| # | 用例 | 期望 |
| - | :--- | :--- |
| B1 | `--robots 100` 运行 | TPS ≈ **1000**（允许 950~1050） |
| B2 | 24 关节键名 | 与 `spec/00 §5.5` **逐键一致**（不止数量） |
| B3 | `max_joint_temp` | = `max(joint_temperatures.values())` |
| B4 | 过热注入 | 运行 30s 后出现 **≥82°C** 关节 |
| B5 | 电量下降 | 1 秒下降 **≈0.05%** |
| B6 | 掉线注入 | 存在 MQTT 静默窗口 |
| B7 | 面板 | rich 每秒刷新，不刷屏 |

## 3. 后端（订单 03）

| # | 用例 | 期望 |
| - | :--- | :--- |
| C1 | 压测 30s 落库 | `robot_telemetry` ≈ **30,000** 条（允许 ±10%） |
| C2 | `/healthz` | 200，且 `mqtt_dropped` 不持续增长 |
| C3 | 队列不积压 | 压测中 `queue_depth` 未长期打满（< 20000） |
| C4 | WS 首帧 | 收到 `type:"snapshot"` 且含 100 台 |
| C5 | WS 增量 | 收到 `type:"telemetry"`，**字段为数字而非字符串** |
| C6 | WS 告警转发 | `redis-cli PUBLISH robot:alerts '{...}'` 后前端收到 `type:"alert"` |
| C7 | REST history | `/robots/humanoid-042/history?minutes=5` 返回非空 `points`，且含 `joint_temperatures` |
| C8 | 非法 resolution | 返回 **400** |
| C9 | 无逐条 INSERT | 代码审查确认使用 `executemany` |

## 4. 告警引擎（订单 04）

| # | 用例 | 期望 |
| - | :--- | :--- |
| D1 | 四条规则齐全 | `motor_temp` / `battery_low` / `offline` / `joint_stuck` 均可触发 |
| D2 | 过热告警 | 压测 30s+ 后 `robot_alert` 出现 `motor_temp` |
| D3 | 低电量告警 | 出现 `battery_low`（电量跌破 15%） |
| D4 | 离线告警 | 停掉某台后 **10s 内**出现 `offline` |
| D5 | 未上线也判离线 | 从未上报的机器人被判离线（非漏判） |
| D6 | 去重幂等 | 同一 `(robot_id, rule_id)` 在窗口内**只写一条** |
| D7 | 告警发布 | `redis-cli SUBSCRIBE robot:alerts` 能收到 |
| D8 | offline 不依赖过期事件 | 代码审查确认使用 ticker + 内存 `last_seen` |

## 5. 前端（订单 05）

| # | 用例 | 期望 |
| - | :--- | :--- |
| E1 | 构建 | `npm run build` **RC=0** |
| E2 | 卡片数 | 渲染 **100** 张 |
| E3 | 无数据占位 | 100 台全离线时显示占位卡，不空白 |
| E4 | 节流生效 | 1000 msg/s 下渲染 ≈ **10 次/秒**（非 1000） |
| E5 | 顶栏 KPI | 在线率/告警数/平均电量数值正确 |
| E6 | Drawer | 打开后 ECharts 折线**有数据**（证明 REST 打通） |
| E7 | 热力图 | 24 个关节块，温区配色正确 |
| E8 | 告警流 | 新告警**置顶**，上限 200 |
| E9 | 断连表现 | 状态点变红，卡片转离线视觉 |
| E10 | 配色 | 严格等于 R-01 §3 token |

## 6. 端到端（订单 06）

| # | 用例 | 期望 |
| - | :--- | :--- |
| F1 | 脚本可重复运行 | 连续跑 2 次均不因残留状态失败 |
| F2 | 三组件等待 | 含 **EMQX** 就绪等待，不出现「连不上 Broker → 0 条」的假失败 |
| F3 | 30s 压测 | `robot_telemetry` ≈ 30,000 |
| F4 | 告警触发 | 过热 + 低电量均出现 |
| F5 | 报告完整 | 含基础设施/后端/压测/telemetry 行数/alert 行数/结论 |
| F6 | 区分错误类型 | 环境错误与代码错误分栏 |

## 7. 全局禁令（任一违反即整批 FAIL）

- 出现 `...` / `// TODO` / 空 `pass` 占位
- 后端逐条 INSERT
- 前端每条 WS 消息重渲染
- `.env` 或密钥被提交 / 烘焙进镜像
- 编造不存在的依赖、类名、端点
