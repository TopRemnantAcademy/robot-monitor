// 前端自报重渲染（订单 16 T2）—— AGENTS.md §3 前端护栏的**唯一证据**。
//
// 【要回答的问题】
// 护栏写着"严禁每条 WS 消息触发 React 重渲染，必须 100ms 节流"。
// 企业验收必然追问：「你凭什么说没重渲染 1000 次/秒？」此前只能嘴说：
// 代码里没有任何计数器，`applyTelemetryBatch` 调了几次、卡片渲染了几次，
// 运行期完全不可见。本模块就是那个"凭什么"。
//
// 【三个计数的语义】—— 三者关系是本模块存在的全部意义
//   telemetryReceived : WS 收到的 telemetry 消息数（≈ 1000/s 稳态）
//   storeCommits      : applyTelemetryBatch 实际被调的次数（≈ 10/s 稳态）
//   cardRenders       : FleetOverviewCards 渲染次数（≈ storeCommits，**不得** ≈ telemetryReceived）
//
//   验收语义（订单 16 §2）：
//     · telemetryReceived / storeCommits ≈ 10  →  100ms 节流生效
//     · cardRenders ≈ storeCommits（而非 ≈ telemetryReceived）→ memo/订阅粒度正确
//   若 storeCommits ≈ telemetryReceived，说明节流被绕过（= 1000 次/秒重渲染）；
//   若 cardRenders ≈ telemetryReceived，说明卡片墙订阅粒度过粗。
//
// 【为什么计数器不能放 React state】
// 放进 useState 本身就是一次 setState → 触发重渲染 → 计数又变 → 再渲染。
// 这会**制造**它本要观测的问题（观测者效应），甚至死循环。
// 因此全部是模块级可变对象 + useRef 自增：读写在渲染路径上零 React 参与。

/** 运行期计数快照。 */
export interface ObservabilitySnapshot {
  /** WS 收到的 telemetry 消息数（含 snapshot 帧，snapshot 也是「收到一条」） */
  wsMsgs: number;
  /** applyTelemetryBatch 实际调用次数（100ms 刷库帧，非空帧才计） */
  storeCommits: number;
  /** FleetOverviewCards 渲染次数（useRef 自增，不触发额外渲染） */
  cardRenders: number;
  /** wsMsgs / storeCommits，节流倍率。稳态应 ≈ 10 */
  ratio: number;
  /** 距上次打印累积的窗口长度（秒），用于把计数换算成速率 */
  windowSeconds: number;
}

// 模块级可变计数器。刻意不用全局 store：本模块是**纯旁观者**，
// 不得引入任何状态管理重构（订单 16 禁止项 4），也不得让组件为它订阅。
const counters = {
  wsMsgs: 0,
  storeCommits: 0,
  cardRenders: 0,
};

/** 上一次打印的 wall-clock（ms）。用 performance.now() 拿单调时钟。 */
let lastReportAt = now();

/** 5s 打印一次的定时器句柄（幂等安装用）。 */
let reportTimer: ReturnType<typeof setInterval> | null = null;

function now(): number {
  // performance.now() 是单调时钟，不受系统时间跳变（NTP 校正）影响 ——
  // 用 Date.now() 的话，时钟回拨会让窗口长度变成负数，速率算出负值。
  return typeof performance !== 'undefined' ? performance.now() : Date.now();
}

/** WS 收到一条 telemetry/snapshot 帧时调用（调用点：useRobotWebSocket）。 */
export function countWsMessage(): void {
  counters.wsMsgs += 1;
}

/** applyTelemetryBatch 被调用时调用（调用点：useRobotWebSocket 的刷库帧）。 */
export function countStoreCommit(): void {
  counters.storeCommits += 1;
}

/**
 * 卡片墙渲染计数。**在组件函数体内直接调用**（配合 useRef 自增语义）。
 *
 * 为什么单独提供函数而不是让组件自己 `ref.current++`：把"怎么计数"收在一处，
 * 将来换口径（比如改成按帧去重）只需改这里，不必翻每个组件。
 * 注意：本函数**不得**触发任何 setState —— 它会在 render 阶段被调用。
 *
 * @param localCount 组件自身 useRef 的当前值（1-based）。仅作交叉校验：
 *        它必须与全局累计同步增长；若某组件被卸载重挂（如切登录），
 *        局部会从 1 重新数而全局继续累加，这个差值本身就是"组件重挂次数"。
 */
export function countCardRender(localCount: number): void {
  counters.cardRenders += 1;
  // 交叉校验：局部值 > 全局值只可能是计数器被误清零（resetObservability 被生产调用）。
  // 用 console.warn 暴露而不是断言抛错 —— 观测模块绝不能把页面搞崩。
  if (localCount > counters.cardRenders) {
    console.warn('[observability] cardRenders local > global', {
      localCount,
      global: counters.cardRenders,
    });
  }
}

/** 只读取快照，不改计数（打印与测试用）。 */
export function getSnapshot(): ObservabilitySnapshot {
  const windowSeconds = Math.max(0, (now() - lastReportAt) / 1000);
  const { wsMsgs, storeCommits, cardRenders } = counters;
  return {
    wsMsgs,
    storeCommits,
    cardRenders,
    // 除零保护：起步阶段 storeCommits 可能为 0（还没刷过帧）。
    // 返回 NaN 会让"≈10"的肉眼判断失效，故 0 提交时 ratio 记 0。
    ratio: storeCommits > 0 ? wsMsgs / storeCommits : 0,
    windowSeconds,
  };
}

/** 清零（测试用；生产不调，否则"累计"语义被破坏）。 */
export function resetObservability(): void {
  counters.wsMsgs = 0;
  counters.storeCommits = 0;
  counters.cardRenders = 0;
  lastReportAt = now();
}

/**
 * 启动 5s 一次的结构化摘要打印。**幂等**：重复调用不会叠加定时器
 * （否则摘要行数翻倍，读的人会误判速率）。
 *
 * 只打印到 console，**不上报任何外网**（订单 16 禁止项 3：工厂内网无外网出口）。
 * 需要留存时由运维从浏览器控制台导出，或后续接内网 OTLP collector。
 *
 * @param intervalMs 打印间隔，默认 5000（订单 16 §1 T2）。测试可注入小值。
 * @returns 停止函数（卸载/测试用）。
 */
export function startObservabilityReport(intervalMs = 5000): () => void {
  if (reportTimer !== null) return stopObservabilityReport;

  reportTimer = setInterval(() => {
    const snap = getSnapshot();
    // 结构化单行：`console.info` 便于按级别过滤，对象形态便于 DevTools 展开。
    // 字段名与订单 16 §1 T2 原文一致（wsMsgs / storeCommits / cardRenders / ratio）。
    console.info('[observability]', {
      wsMsgs: snap.wsMsgs,
      storeCommits: snap.storeCommits,
      cardRenders: snap.cardRenders,
      ratio: Number(snap.ratio.toFixed(2)),
    });
  }, intervalMs);

  return stopObservabilityReport;
}

/** 停止摘要打印。未启动时是 no-op。 */
export function stopObservabilityReport(): void {
  if (reportTimer !== null) {
    clearInterval(reportTimer);
    reportTimer = null;
  }
}
