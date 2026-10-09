# R-02 WebSocket 接入与 100ms 节流需求（前端 · 防卡顿核心）

> 与 `R-01` 配套。本文件规定 `src/hooks/useRobotWebSocket.ts` 的**行为契约**。
> 背景：全局 100 台 × 10Hz = **1000 msg/s**。若每条消息触发 React 重渲染，界面必然卡死。

## 1. 连接

- 地址：`ws://${import.meta.env.VITE_WS_HOST ?? 'localhost'}:8000/ws/robots`
- 使用**原生 `WebSocket`**，禁止引入 socket.io / reconnecting-websocket 等库。
- Hook 在 `App` 挂载时建立连接，卸载时关闭并清理定时器（**必须清理，否则内存泄漏**）。

## 2. 连接状态机

| 状态 | 含义 | UI 表现（`HealthSummaryHeader` 状态点） |
| :--- | :--- | :--- |
| `connecting` | 首次连接中 | 黄 |
| `open` | 已连接 | 绿 |
| `reconnecting` | 断开重试中 | 红 |
| `closed` | 主动关闭（卸载） | 灰 |

状态存于 Zustand `connected`（布尔）+ 组件本地 `reconnecting`。

## 3. 自动重连（指数退避）

- `onclose` / `onerror` 后重连，退避序列：`1s → 2s → 4s → 8s → 10s → 10s …`（**上限 10s**）。
- 建议加 **±20% 随机抖动**，避免服务端重启时所有客户端同时重连造成惊群。
- 重连成功（`onopen`）后重置退避计数。
- 页面 `visibilitychange` 重新可见时可立即尝试重连（可选优化）。

## 4. 消息分发（按 `type` 三分支，强制）

```ts
ws.onmessage = (ev) => {
  let msg: any;
  try { msg = JSON.parse(ev.data); } catch { malformedCount++; return; }  // 解析失败：跳过并计数
  switch (msg.type) {
    case 'snapshot':  latestRef.current.clear();
                      for (const r of msg.data.robots) latestRef.current.set(r.robot_id, r);
                      flushNow();                       // 快照立即刷一次，避免首屏空 100ms
                      break;
    case 'telemetry': latestRef.current.set(msg.data.robot_id, msg.data); break;  // 只入缓冲
    case 'alert':     pushAlert(msg.data); break;       // 告警走独立通道，不受节流影响
    default:          unknownTypeCount++;               // 未知 type：忽略并计数
  }
};
```

- **告警不受 100ms 节流约束**：告警低频且需即时可见，收到即 `pushAlert`。
- 未知 `type` 忽略并计数，便于排查后端/前端版本不一致。

## 5. 100ms 节流（硬性要求）

```ts
const latestRef = useRef<Map<string, RobotTelemetry>>(new Map());

useEffect(() => {
  const timer = setInterval(() => {
    if (latestRef.current.size === 0) return;
    setRobots(Array.from(latestRef.current.values()));  // 批量写入 Zustand
    latestRef.current.clear();
  }, 100);
  return () => clearInterval(timer);                    // 必须清理
}, []);
```

**必须在代码注释中写明原因**：
> 100 台 × 10Hz = 1000 msg/s。若每条消息直接 setState，React 每秒需重渲染 1000 次，
> 主线程必然阻塞。改为 `useRef` 缓冲 + 每 100ms 批量刷入 Zustand，
> 渲染次数从 1000 次/秒 降至 **10 次/秒**，同时数据新鲜度仍保持 100ms 内。

**禁止**：
- 禁止在 `onmessage` 里调用 `setRobots` / `setState`。
- 禁止对 100 张卡片逐张订阅 store 导致 100 次重渲染（应整体批量更新）。

## 6. 背压与异常

- `onmessage` 内**不做重活**（不解析大 JSON 之外的计算、不发请求）。
- 若 `latestRef` 因异常增长（如 robot_id 不受控），需限制 Map 上限（建议 1000）并告警计数。
- 连接断开期间，已有卡片保留最后状态并整体转为离线视觉（由 `connected=false` 驱动）。

## 7. 验收（可操作）

| # | 用例 | 期望 |
| - | :--- | :--- |
| T1 | 后端以 1000 msg/s 推送 10s | 页面流畅；React 渲染次数 ≈ **100 次**（10s × 10 次/s），而非 10000 次 |
| T2 | 手动断开 WS | 状态点变红，按 1/2/4/8/10s 退避重连 |
| T3 | 重连成功 | 收到 `snapshot`，100 张卡恢复，状态点转绿 |
| T4 | 推送非法 JSON | 不计入崩溃，`malformedCount` +1，界面不受影响 |
| T5 | 推送未知 `type` | 忽略，不影响渲染 |
| T6 | 组件卸载 | `clearInterval` 已调用，WS 已 `close()`，无残留定时器 |

验证 T1 的实用方法：在 `setRobots` 处加临时计数器打印，或在 DevTools Performance 面板确认无 1000 次/秒 的 commit。
