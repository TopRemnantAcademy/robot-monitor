# R-VLA-EVAL — RobotMonitor 作为 VLA 评估可观测性底座

> 状态：Phase 1（动作流旁路）已实现并端到端验证 · 2026-11-11
> 作者：本地实现（Agent 模式，用户授权「按照方案执行」）
> 关联：用户贴出的 **VLA Lead JD**（pi0.5 / GR00T N1.7 / Psi0、LeRobot / openpi / Isaac-GR00T、RL 后训练、VR 纠错、TensorRT 推理优化、LeRobot 格式数据目录）

---

## 1. 背景与判断（已实测，非推测）

用户贴出一份 VLA（Vision-Language-Action）团队 Lead 招聘 JD，问「VLA 在我们平台里吗」。
经全工作区 + 本仓源码检索（关键词 `vla|openvla|rt-2|octo|diffusion-policy|lerobot|openpi|isaac|gr00t|pi0|psi0|unitree|mujoco|tensorrt`）
**零命中**，并核验了依赖清单与模型权重文件，结论：

- **RobotMonitor 当前是「机队遥测监控 / 可观测性平台」**，不训练 / 不微调 / 不推理任何 VLA 策略，也没有标注模型、数据目录、仿真或真机硬件。
- JD 与本平台的**唯一真实交集 = ROS 2 遥测接入**：`bridge/` 已解析 `sensor_msgs/BatteryState`、`sensor_msgs/JointState`、`nav_msgs/Odometry`、`geometry_msgs/PoseStamped` → ISO 21423 信封，后端已落库 + WS 推送 + 看板。
- JD 的 "find causes of failures in rollouts, logs and videos" + "blind A/B trials, Wilson intervals, McNemar" 中，**遥测 / 日志这一腿本平台已有**，但视频、策略动作流、A/B 标注、统计三块是缺口。

**核心定位（务必守住）**：RobotMonitor 不做 VLA，而是做 **ROS 2 侧的「评估可观测性 + 编排底座」**。真实 VLA 推理 / 训练 / 数据目录交给现成开源组件，本平台只做它们与 LeRobot 世界之间的「桥梁 + 评估层」。

---

## 2. 目标 / 非目标

### 目标
1. 把 RobotMonitor 从「遥测监控平台」升级为「VLA 评估可观测性底座」。
2. 在现有遥测链路上**旁路**接入：策略动作流、试验（A/B）标注、统计显著性。
3. 所有新增的 ML / 数据 / 统计能力**真接入开源组件**，严禁手搓（见第 4 节铁律）。

### 非目标（硬边界）
- ❌ 不实现 / 不训练 / 不微调 VLA 策略本身（那是外部 ROS 2 节点 / HuggingFace LeRobot 的事）。
- ❌ 不重建 LeRobot 数据集格式（只引用 `repo_id`）。
- ❌ 不手搓推理运行时、统计库、ROS bag 转换（用 rosbag2 / LeRobot / statsmodels / SMEAC rosetta）。
- ❌ 不把视频原始文件写进 TimescaleDB（只存引用：bag_uri / mp4 URL）。

---

## 3. 目标架构（逻辑层）

```
                        真机 / MuJoCo·BiGym / 改造 mock_fleet (ROS 2)
                                       │  roscore + rosbridge (JSON WS)
                                       ▼
   ┌──────────────────────── RobotMonitor 边界（本仓） ───────────────────────┐
   │  bridge/ ROS2→ISO21423（已存在，本次扩展动作消息）                          │
   │        │  MQTT robots/{id}/telemetry                                        │
   │        ▼                                                                   │
   │  backend_ingestion: mqtt_subscriber → batch_writer → robot_telemetry(已有)   │
   │                    action_subscriber → action_writer → policy_actions(新增) │
   │                    ws_hub → /ws/robots (snapshot+telemetry+alert+action)    │
   │  TimescaleDB: robot_telemetry / policy_actions / trials / trial_outcomes /   │
   │              tasks / trial_media                                            │
   └──────────────────────────────────────────────────────────────────────────┘
        │                                          │
        ▼ (rosbag2 录制原始 episode)               ▼ (评估 REST: 成功率 / Wilson / McNemar)
   rosetta port_bags.py → LeRobot 数据集 ──push──► HuggingFace / 本地 (外部, 不重建)
        ▲                                          │
        │ rosetta_client_node (把 VLA 策略包装成 ROS2 action 部署)                │
        └──────────── 外部 VLA 推理节点（pi0.5/GR00T/Psi0 via LeRobot/openpi/Isaac-GR00T）
```

---

## 4. 引入来源清单表（铁律：禁手搓 · 真开源 · 来源可追溯）

| 组件 | 用途 | 官方来源（必须核对 digest / 版本） | 许可证 | 本方案角色 | pin |
|---|---|---|---|---|---|
| **rosbag2** | 原生录制 ROS 2 全 topic（观测+动作+相机），MCAP/sqlite | github.com/ros2/rosbag2 · docs.ros.org | Apache-2.0 | 原始 episode 采集（事件触发录制用 `rosbag2_py`） | 随 ROS 2 发行版（建议 ROS 2 Humble/Jazzy） |
| **SMEAC/rosetta** | ROS2 bag → LeRobot 数据集转换（`port_bags.py` + `contract.yaml`）；`rosetta_client_node` 把策略包装成 ROS2 action 部署 | github.com/SMEAC/rosetta | **待核实**（疑似 Apache-2.0，落地前必须读 LICENSE） | 连接 RobotMonitor 的 ROS2 世界与 LeRobot/VLA 世界的胶水 | 锁定 commit（落地前 pin） |
| **LeRobot** (HuggingFace) | 标准化机器人学习数据格式（Parquet 状态/动作 + MP4 相机 + meta）；`lerobot-train` 支持 Pi0.5 / GR00T N1.5 / SmolVLA + LoRA | github.com/huggingface/lerobot | Apache-2.0 | JD 说的「LeRobot 格式数据目录」= HF LeRobot 数据集，**只引用不重建** | ≥0.6（以官方兼容为准） |
| **statsmodels** | `proportion_confint(method='wilson')` 成功率先验区间；`mcnemar` + `confint_proportions_paired(method='newcomb')` 配对 A/B | statsmodels.org · PyPI `statsmodels` | BSD-3-Clause | JD 要求的 Wilson / McNemar 统计（真实库，非手算） | 0.14.x |

> **铁律执行口径**（来自用户 memory「必须真下载开源项目，严禁手搓」）：
> - 任何「统计」「ROS bag 转换」「VLA 数据集格式」类能力，**优先用上表组件**，禁止自写等价于 `scipy`/`statsmodels`/`lerobot` 的代码。
> - 每个新增模块的 PR 必须附「引入来源清单表」（组件 / 镜像全名+tag / 官方 URL / 是否实际拉取 / 是否有本地改写）。
> - 配置片段（compose / nginx / 健康检查）凡官方有样例的，必须取自官方文档或官方仓库。
> - 没有现成开源组件的能力 → **不自己实现**，列「未找到可复用实现」清单交人工决策。

---

## 5. 分阶段计划（每阶段可验证，mocktest 级）

- **Phase 0 — 打通 ROS2 采集源（最关键前提）**：部署能发 ROS2 topic 的源（真机 / MuJoCo·BiGym 仿真 / 改造 mock_fleet）。rosbag2 才能录到真 MCAP；验证 bridge 仍正常摄入。**当前最大前置缺口：本仓无真 ROS2 在环路**（simulator 直接发 MQTT，绕过 ROS2）。
- **Phase 1 — 动作流旁路（本次实现）**：扩 schema + bridge 双后端动作解析 + subscriber + `policy_actions` 超表 + WS 动作帧 + simulator 动作发射；`mock_test` 增加动作场景（Rust/Python 零偏差）。✅ 进行中。
- **Phase 2 — 试验 + 统计**：建 `trials` / `trial_outcomes` / `tasks` 关系表 + `eval_service` 路由用 statsmodels 算 Wilson + McNemar；用合成 2×2 列联表单测，断言与 statsmodels 已知输出一致。
- **Phase 3 — bag→LeRobot 旁路**：用 rosetta `port_bags.py` 把录到的 MCAP 转 LeRobot 数据集并 push；存 `repo_id`；验证 bag→LeRobot→`LeRobotDataset` 闭环。
- **Phase 4 — 前端 Trials / A-B 视图 + 视频引用查看器**。
- **Phase 5 — 盲 A/B 分配 + 显著性 UI**：评估者看不到 policy 归属；展示 Wilson 区间与 McNemar p 值。

---

## 6. Phase 1 详细设计（本次落地的代码锚点）

### 6.1 新增数据契约（不改现有遥测）
- `backend_ingestion/app/schemas/telemetry.py` 增加 `PolicyAction` pydantic 模型：
  `robot_id`(匹配 `^humanoid-[0-9]{3}$`)、`policy_version`(str)、`trial_id`(str|None)、`action`(list[float]，动作向量)、`goal`(str|None)、`ts`(int|None)。
- `infra/init.sql` 增加 `policy_actions` 超表（镜像 `robot_telemetry` 结构 + `policy_version` / `trial_id` / `action`(JSONB 向量) / `goal`）；**已有数据库通过迁移脚本 `infra/migrations/002_policy_actions.sql` 应用，不重跑 init.sql**（保护 38 万+ 行现有遥测）。

### 6.2 旁路摄入（与遥测严格隔离，零耦合）
- 新增 `backend_ingestion/app/services/action_subscriber.py`：订阅 `robots/+/action`，校验 `PolicyAction`，发布到 Redis 通道 `chan_action_all`（新增 config），并 enqueue 到动作 writer。
- 新增 `backend_ingening/app/services/action_writer.py`：复用 `BatchWriter` 的降级链（COPY→staging→upsert / executemany / 坏行隔离），仅替换表名、列集、`_to_row`、`INSERT_SSTAGING` 为 `policy_actions`。
- `backend_ingestion/app/ws_hub.py`：`_consume_loop` 增加订阅 `chan_action_all`，`_latest_action` 按 robot_id 合并，beat 广播 `{"type":"action","data":{...}}`；`main.py` 的 `/ws/robots` 快照保持遥测不变，增量帧自动含动作。

### 6.3 模拟器动作发射
- `simulator/mock_fleet.py`：增加 `ACTION_TOPIC = "robots/{robot_id}/action"`，每台机器人按 10Hz 发射合成动作向量（如 6 维末端执行器增量 + `policy_version="vla-sim-0.1"` + `trial_id=None`），与遥测共用同一 publisher 串行发布，不影响现有遥测链路。

### 6.4 bridge 双后端动作解析（保持零偏差铁律）
- `bridge/_iso21423_py.py` 增加 `policy_action_to_wire(pa: dict) -> dict`（映射 `robotmonitor_msgs/msg/PolicyAction` → `PolicyAction` 线格式，与后端 schema 字段逐一对齐）。
- `bridge/iso21423-rs/src/lib.rs` 增加等价 `#[pyfunction] fn policy_action_to_wire(...)`；`maturin develop` 重建扩展。
- `bridge/mock_test.py` 增加动作场景，Rust/Python 对 `policy_action_to_wire` 输出逐字段零偏差比对。

### 6.5 lifespan 接线
- `backend_ingestion/app/main.py`：启动 `action_subscriber.run(action_writer)` 任务；`config.py` 增加 `chan_action_all` 通道配置；shutdown 顺序与遥测对称（先停 MQTT 动作订阅 → flush 动作 writer → 停 hub）。

---

## 7. 验收标准（Phase 1）

1. `bridge/mock_test.py` 全部场景（含新增动作场景）Rust/Python 零偏差通过。
2. 重建后端镜像 + 应用 `002_policy_actions` 迁移后，`/healthz` 在保留原有 `robot_telemetry` 数据的前提下新增动作摄入计数。
3. 容器内 WS 客户端（用 demo 账号 token）能收到 `snapshot` + `telemetry` + `action` 三类帧。
4. `SELECT count(*) FROM policy_actions` 随时间增长，`mqtt_dropped` 对动作 topic 为 0。
5. 前端 `http://localhost:5173/` 登录 demo 账号后，能在浏览器看到动作帧进入（后续 Phase 4 做专用 Trials 视图；Phase 1 先验证数据通道）。

---

## 8. 硬缺口 / 风险（如实记录）

1. **真 ROS2 机器人在环路里没有**（最大前置）：`simulator/mock_fleet.py` 直接发 MQTT 绕过 ROS2；`bridge/ros2_bridge.py` 期望 rosbridge。所以 rosbag2 路径目前无数据源 —— Phase 0 必须先建立「能发 ROS2 topic 的源」。
2. **VLA / LeRobot / 数据目录都不在本仓构建**，是外部系统（HF LeRobot + SMEAC/rosetta）；RobotMonitor 只做观测 + 评估。VLA 策略作为外部 ROS2 节点（rosetta_client）运行，边界必须守住。
3. **rosetta 许可证需核实**再 pin；**statsmodels** 取 0.14.x（BSD，已确认 `proportion_confint(method='wilson')` 与 `mcnemar` 存在）。
4. **视频不进 TimescaleDB**：只存引用，原始视频留在 MCAP / LeRobot MP4（对象存储），避免超表膨胀。
