# R-01 — 前端监控大屏构造规格

> 状态：订单 05 **已实现并合入 main**（`frontend_dashboard/`），
> 主代理已实测 `tsc --noEmit` 0 错误、`vite build` 成功。
> 本文件是**回归验收基线**：后续任何前端改动不得违反本文规定的契约与禁止项。

## 1. 设计令牌（`src/theme/tokens.ts`）

| 令牌 | 值语义 | 用途 |
|---|---|---|
| `COLORS.statusNormal` | 正常（status_code=1） | 卡片状态点 |
| `COLORS.statusWarning` | 预警（2） | 同上 |
| `COLORS.statusFault` | 故障（3） | 同上 |
| `COLORS.statusOffline` | 离线（0） | 同上 + 无数据回落 |
| `COLORS.textPrimary` / `textSecondary` | 主/次文本 | 全局 |
| `FONT.cardId` / `FONT.metric` | 卡片 ID / 数值字号 | 排版一致 |

**温度色阶 `tempColor(t)`**：低温→高温渐变，超出阈值走故障色。
规则：中国区习惯不做红涨绿跌映射（本项目非金融），高温用**暖色/红**，符合直觉。

## 2. 三区布局

```
┌─────────────────────────────────────────────────┐
│ HealthSummaryHeader  在线率 / 故障数 / 告警数 / WS 状态点 │
├─────────────────────────────────────────────────┤
│ FleetOverviewCards   100 张机器人卡片（网格，可滚动）      │
├─────────────────────────────────────────────────┤
│ AlertLogTable        告警流，新告警置顶                │
└─────────────────────────────────────────────────┘
        点击任意卡片 → RobotDetailDrawer（右侧抽屉）
```

## 3. 组件规格

### 3.1 HealthSummaryHeader（88 行）
- 统计口径：`status_code !== 0` 计为在线（见 `store/robotStore.ts:111`）
- 分母**固定 100**（`buildRobotIds(100)`），不随已收数据台数变化
- WS 状态点三态：`connecting`(黄) / `open`(绿) / `closed`(红)

### 3.2 FleetOverviewCards（92 行）
- 每张卡：机器人 ID、状态色点、最高关节温、电量、状态文案
- 离线时温度显示 `--` 而非 `0°C`（避免把"无数据"误读为"0 度"）
- 点击 → `selectRobot(id)`

### 3.3 RobotDetailDrawer（249 行）
- 三条 ECharts 折线：**温度(°C) / 扭矩(N·m) / 角度(rad)**
- 数据源 `GET /robots/{id}/history?minutes=5&resolution=10s`
- 关节下拉选择（24 个标准名，`JOINT_NAMES`）
- **关键约束**：第 3 条「角度(rad)」依赖 `point.joint_positions[joint]`，
  后端必须返回该字段（`backend_ingestion/app/main.py` 已于 2026-10-10 补齐），
  否则该曲线恒为 null、图上空白

### 3.4 JointHeatmap（74 行）
- 24 关节温度网格，键名必须取自 `JOINT_NAMES`（spec/00 §5.5 唯一事实来源）
- 空机器人也要渲染 24 格（占位为 0），不得渲染空网格

### 3.5 AlertLogTable（88 行）
- 新告警 prepend，缓冲上限 `ALERT_BUFFER_LIMIT = 200`
- 展示 robot_id / rule_id / severity / message / 时间

## 4. 状态管理（`src/store/robotStore.ts`）

```
robots: Map<string, RobotTelemetry>   // 100 台常驻，未上报为离线占位
alerts: RobotAlert[]                  // 上限 200
wsStatus: 'connecting' | 'open' | 'closed'
selectedRobotId: string | null
```

- `applySnapshot(list)` — 全量覆盖，缺失 ID 回落占位
- `applyTelemetryBatch(list)` — **批量唯一入口**，被 100ms 节流调用
- `pushAlert(alert)` — prepend + 截断

## 5. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `VITE_WS_HOST` | `localhost` | 也接受 `ws://host:port` 形式 |
| `VITE_WS_PORT` | `8000` | 仅当 HOST 未带端口时拼接 |
| `VITE_API_BASE` | 同源 | REST 基址 |

**禁止**使用非 `VITE_` 前缀变量（Vite 不会注入）。

## 6. 禁止项（回归时逐条检查）

1. 禁止在 `onmessage` 里直接 `setState`（必须走 ref 缓冲 + 100ms 节流）
2. 禁止把 `joint_*` 字典键名写死（必须引用 `JOINT_NAMES`）
3. 禁止把离线（status_code=0）渲染成 `0°C`
4. 禁止告警流无上限增长
5. 禁止硬编码机器人数量（用 `buildRobotIds(100)`）

## 7. 验收清单（10 项）

- [ ] 首屏未连 WS 时也有 **100 张**卡片（离线占位）
- [ ] 卡片的 status_code → 状态色映射正确（非字符串 `"1"`）
- [ ] 顶栏在线率分母 = 100
- [ ] WS 断线时状态点变红
- [ ] 重连退避 1→2→4→8→10s（封顶）
- [ ] Drawer 三条折线**均有数据点**
- [ ] 关节热力图渲染 24 格
- [ ] 告警新进置顶
- [ ] 1000 msg/s 下页面不卡顿（store 更新 ≈10 次/秒）
- [ ] `tsc --noEmit` 0 错误、`vite build` 成功
