// spec/03 §5.3 —— 单机关节热力诊断 Drawer（右滑 480px，不挤压主区）
//
// 数据来源：
//   1) 实时增量：直接从 Zustand 的 robots Map 取（该机器人 10Hz 更新）
//   2) 历史折线：GET {VITE_API_BASE}/robots/{id}/history?minutes=5&resolution=10s（spec/00 §5.3）

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
// 订单 09：走 echarts-for-react 的 core 入口 + 按需注册的 echarts 实例，
// 取代默认入口（默认入口内部 require 整个 echarts 包，无法 tree-shake）。
// 注册清单见 src/lib/echartsCore.ts（只含本 Drawer 用到的折线图相关模块）。
import ReactEChartsCore from 'echarts-for-react/lib/core';
import echarts from '../lib/echartsCore';
import JointHeatmap from './JointHeatmap';
import {
  COLORS,
  DISPLAY_COLOR,
  DISPLAY_LABEL,
  FONT,
  hasEverReported,
  robotDisplayState,
  severityColor,
} from '../theme/tokens';
import { useRobotStore } from '../store/robotStore';
import { JOINT_NAMES } from '../types/robot';
import type { AlertApiItem, HistoryPoint } from '../types/robot';
import { ruleLabel } from './AlertLogTable';
import { ackAlert, fetchAlerts, unackAlert } from '../auth/alertsApi';
import { useIsAdmin } from '../auth/authStore';
import {
  HISTORY_MINUTES,
  HISTORY_RESOLUTION,
  HISTORY_WINDOW_OPTIONS,
  fetchRobotHistory,
  robotHistoryKey,
} from '../api/robotHistory';

// 订单 26：Drawer 不再自持 REST 基址，统一从 auth/api 取（同时 re-export 兼容既有引用点）。
import { API_BASE } from '../auth/api';

export { API_BASE };

/**
 * 「最后更新」的毫秒时间戳：优先后端的 `last_seen`（接收侧事实，秒 → 毫秒），
 * 缺失时才回落到机器人自述的 `ts`（毫秒）。两者都没有 → null（显示 `--`）。
 */
function lastSeenMs(robot: { last_seen?: number | null; ts: number } | undefined): number | null {
  if (!robot) return null;
  if (typeof robot.last_seen === 'number') return robot.last_seen * 1000;
  return robot.ts > 0 ? robot.ts : null;
}

/** 「数据帧龄」：last_seen 距现在的时长（失联诊断用——「3 小时前」比一个时刻数字直观）。 */
function frameAge(ms: number | null): string {
  if (ms === null) return '--';
  const sec = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (sec < 60) return `${sec} 秒前`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  return `${Math.floor(min / 60)} 小时前`;
}

interface ChartSeries {
  name: string;
  data: (number | null)[];
}

interface ChartModel {
  xAxis: string[];
  series: ChartSeries[];
  /** 电量逐桶（HistoryPointOut.battery_pct），供独立电量趋势图使用 */
  battery: (number | null)[];
}

/** 折线用色：温度=故障红 / 扭矩=强调蓝 / 角度=正常绿 / 包络=预警黄（全 tokens，不自创色相）。 */
const SERIES_COLORS = [COLORS.statusFault, COLORS.accent, COLORS.statusNormal, COLORS.statusWarning];

export default function RobotDetailDrawer() {
  const selectedRobotId = useRobotStore((s) => s.selectedRobotId);
  const selectRobot = useRobotStore((s) => s.selectRobot);
  // ISSUE #75 结构性共享：Drawer 只订阅**它显示的那一格**
  // （`s.robots.get(selectedRobotId)`），不再订阅整张 Map —— 否则关着 Drawer 时
  // 每个 100ms 刷库帧也会让它重渲染一遍。选中态切换 / 关闭时选择器自动换挡。
  const robot = useRobotStore((s) => (s.selectedRobotId ? s.robots.get(s.selectedRobotId) : undefined));

  const [selectedJoint, setSelectedJoint] = useState<string>(JOINT_NAMES[0]);
  // 订单 27 T3：历史窗口（分钟）。进 queryKey —— 切窗口即换 key，react-query 自动重拉。
  const [historyMinutes, setHistoryMinutes] = useState<number>(HISTORY_MINUTES);
  // 订单 27 T2：告警确认（仅 admin）。busyId 与错误信息只作用于本 Drawer 的告警区。
  const [ackBusyId, setAckBusyId] = useState<number | null>(null);
  const [ackError, setAckError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const canAck = useIsAdmin();

  // 每次打开 Drawer 重置选中关节，避免残留上一台机器人的选择
  useEffect(() => {
    if (selectedRobotId) setSelectedJoint(JOINT_NAMES[0]);
  }, [selectedRobotId]);

  // 订单 26（I-20）：历史查询交给 react-query 管缓存与重试。
  //   - 同机器人重复打开：staleTime 内直接命中缓存，不打后端；
  //   - 失败自动重试（4xx 除外，见 lib/queryClient.ts）；
  //   - 切换机器人 / 关闭 Drawer 时 react-query 自动取消在途请求（signal 透传给 fetch）。
  const historyQuery = useQuery({
    queryKey: robotHistoryKey(selectedRobotId ?? '', historyMinutes),
    queryFn: ({ signal }) => fetchRobotHistory(selectedRobotId as string, signal, historyMinutes),
    enabled: selectedRobotId !== null,
  });

  const history: HistoryPoint[] = historyQuery.data ?? [];
  // enabled=false（未选中机器人）时 isPending 恒为 true 但并未发请求，故用 fetchStatus 区分。
  const loading = historyQuery.isPending && historyQuery.fetchStatus !== 'idle';
  const error = historyQuery.isError
    ? historyQuery.error instanceof Error
      ? historyQuery.error.message
      : 'history 请求失败'
    : null;

  // ECharts 折线模型：温度/扭矩/角度 三条 series，x 轴为 10s 桶时间
  const chart = useMemo<ChartModel>(() => {
    const xAxis: string[] = [];
    const temp: (number | null)[] = [];
    const torque: (number | null)[] = [];
    const position: (number | null)[] = [];
    const maxTemp: (number | null)[] = [];
    const battery: (number | null)[] = [];

    for (const point of history) {
      const t = new Date(point.time);
      xAxis.push(
        `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}:${String(t.getSeconds()).padStart(2, '0')}`,
      );
      const tv = point.joint_temperatures?.[selectedJoint];
      const tq = point.joint_torques?.[selectedJoint];
      const po = point.joint_positions?.[selectedJoint];
      temp.push(typeof tv === 'number' ? Number(tv.toFixed(2)) : null);
      torque.push(typeof tq === 'number' ? Number(tq.toFixed(2)) : null);
      position.push(typeof po === 'number' ? Number(po.toFixed(3)) : null);
      // 全关节最高温包络（后端逐桶聚合 max_joint_temp，非客户端推算）：
      // 单看选中关节会漏「别的关节在发烧」——包络抬升即整机热问题。
      maxTemp.push(
        typeof point.max_joint_temp === 'number' ? Number(point.max_joint_temp.toFixed(2)) : null,
      );
      battery.push(typeof point.battery_pct === 'number' ? Number(point.battery_pct.toFixed(2)) : null);
    }
    return {
      xAxis,
      series: [
        { name: '温度(°C)', data: temp },
        { name: '扭矩(N·m)', data: torque },
        { name: '角度(rad)', data: position },
        { name: '全关节最高温(°C)', data: maxTemp },
      ],
      battery,
    };
  }, [history, selectedJoint]);

  // 该机器人最近告警：REST 是权威视图（含 ack/escalated 事后状态，WS 只推增量）。
  // 后端 /alerts 暂无 robot_id 查询参数，故拉最近 200 条（_MAX_LIMIT=500 内）客户端过滤。
  //
  // 订单 35：这条查询**包含**被抑制项（`suppressed` 刻意不传）。
  // 与 AlertLogTable 默认排除的做法不同，理由是本区的用途 ——
  // 它是"排障时看这台机器到底报了些什么"，被压住的行正是最需要看见的那一批
  //（它们解释了"为什么没弹出来"）。所以这里要全集，并在行内标「已抑制」。
  const alertsQuery = useQuery({
    queryKey: ['robot-alerts', selectedRobotId],
    queryFn: () => fetchAlerts({ limit: 200 }),
    enabled: selectedRobotId !== null,
  });
  const robotAlerts: AlertApiItem[] = useMemo(() => {
    const items = alertsQuery.data?.items;
    if (!Array.isArray(items)) return [];
    return items.filter((a) => a.robot_id === selectedRobotId).slice(0, 5);
  }, [alertsQuery.data, selectedRobotId]);

  // 订单 27 T1：24 关节实时扭矩负载行模型（纯 CSS 条，无新 ECharts 实例）。
  // maxAbs 取当前帧有值关节的最大 |扭矩| 作为满刻度；全零/无数据时钳到 1 防除零，
  // 此时所有条宽为 0——语义仍是「无负载」而不是报错。
  const torqueRows = useMemo(() => {
    const torques = robot?.joint_torques ?? {};
    let maxAbs = 0;
    for (const j of JOINT_NAMES) {
      const v = torques[j];
      if (typeof v === 'number') maxAbs = Math.max(maxAbs, Math.abs(v));
    }
    if (maxAbs < 1e-9) maxAbs = 1;
    return JOINT_NAMES.map((joint) => {
      const v = torques[joint];
      const value = typeof v === 'number' ? v : null;
      return {
        joint,
        value,
        pct: value === null ? 0 : Math.min(100, (Math.abs(value) / maxAbs) * 100),
      };
    });
  }, [robot?.joint_torques]);

  // 订单 27 T2：确认/撤回（仅 admin 可见，403 由后端兜底——前端只是不让看见点不动的东西）。
  // 成功后 invalidate 本 Drawer 的告警查询：REST 是 ack 状态的权威视图，重拉即对账。
  const handleToggleAck = useCallback(
    async (a: AlertApiItem) => {
      setAckBusyId(a.alert_id);
      setAckError(null);
      try {
        await (a.ack ? unackAlert(a.alert_id) : ackAlert(a.alert_id));
        await queryClient.invalidateQueries({ queryKey: ['robot-alerts', selectedRobotId] });
      } catch (e) {
        // 刻意不透传含 "HTTP 5xx" 的原始文案：500 用例里 getByText(/HTTP 500/)
        // 必须唯一命中历史图错误（测试契约），且 5xx 对值班员无操作价值。
        const msg = e instanceof Error ? e.message : '操作失败';
        setAckError(msg.includes('HTTP 5') ? '告警确认操作失败' : msg);
      } finally {
        setAckBusyId(null);
      }
    },
    [queryClient, selectedRobotId],
  );

  if (!selectedRobotId) return null;

  const chartOption = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: {
      data: chart.series.map((s) => s.name),
      textStyle: { color: COLORS.textSecondary, fontSize: 11 },
      top: 0,
    },
    grid: { left: 44, right: 16, top: 32, bottom: 24 },
    xAxis: {
      type: 'category',
      data: chart.xAxis,
      axisLine: { lineStyle: { color: COLORS.border } },
      axisLabel: { color: COLORS.textSecondary, fontSize: 10 },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLine: { lineStyle: { color: COLORS.border } },
      splitLine: { lineStyle: { color: COLORS.border } },
      axisLabel: { color: COLORS.textSecondary, fontSize: 10 },
    },
    series: chart.series.map((s, idx) => ({
      name: s.name,
      type: 'line' as const,
      smooth: true,
      showSymbol: chart.xAxis.length <= 2,
      connectNulls: true,
      data: s.data,
      lineStyle: { width: 2, color: SERIES_COLORS[idx] },
      itemStyle: { color: SERIES_COLORS[idx] },
    })),
    animation: false,
  };

  // 电量趋势：卡片墙/基本信息只有瞬时电量，「5 分钟掉电 >10%」这类速率异常在这里一眼可见。
  // 数据为后端逐桶聚合的 battery_pct（HistoryPointOut），非客户端推算。
  const batteryChartOption = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    grid: { left: 36, right: 12, top: 10, bottom: 22 },
    xAxis: {
      type: 'category',
      data: chart.xAxis,
      axisLine: { lineStyle: { color: COLORS.border } },
      axisLabel: { color: COLORS.textSecondary, fontSize: 10 },
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      axisLine: { lineStyle: { color: COLORS.border } },
      splitLine: { lineStyle: { color: COLORS.border } },
      axisLabel: { color: COLORS.textSecondary, fontSize: 10 },
    },
    series: [
      {
        name: '电量(%)',
        type: 'line' as const,
        smooth: true,
        showSymbol: chart.xAxis.length <= 2,
        connectNulls: true,
        data: chart.battery,
        lineStyle: { width: 2, color: COLORS.accent },
        itemStyle: { color: COLORS.accent },
        // accent #3B82F6 的 12% 透明派生（同 tokens.overlay 的派生方式，不引入新色相）
        areaStyle: { color: 'rgba(59, 130, 246, 0.12)' },
      },
    ],
    animation: false,
  };

  // ISSUE #77：Drawer 头部的状态同样只认 is_online。
  // 旧写法 `STATUS_COLOR[robot.status_code]` 会让一台停报的"预警"机器人
  // 在 Drawer 里继续保持黄色 —— 与卡片墙上的「失联」灰自相矛盾。
  const displayState = robot
    ? robotDisplayState(robot.is_online === true, robot.status_code, hasEverReported(robot))
    : 'offline';
  const statusColor = robot ? DISPLAY_COLOR[displayState] : COLORS.statusOffline;
  const statusText = robot ? DISPLAY_LABEL[displayState] : '无数据';

  return (
    <>
      {/* 遮罩层：点击关闭（不阻挡主区布局，Drawer 固定定位） */}
      <div
        className="fixed inset-0 z-30"
        style={{ backgroundColor: COLORS.overlay }}
        onClick={() => selectRobot(null)}
      />
      <aside
        className="fixed right-0 top-0 h-screen z-40 flex flex-col overflow-hidden"
        style={{
          width: '480px',
          backgroundColor: COLORS.surface,
          borderLeft: `1px solid ${COLORS.border}`,
          borderRadius: 0,
        }}
      >
        <div
          className="h-14 shrink-0 flex items-center justify-between px-4"
          style={{ borderBottom: `1px solid ${COLORS.border}` }}
        >
          <div className="flex items-baseline gap-2 min-w-0">
            <span style={{ ...FONT.title, color: COLORS.textPrimary }}>{selectedRobotId}</span>
            <span style={{ ...FONT.metric, color: statusColor }}>{statusText}</span>
          </div>
          <button
            type="button"
            onClick={() => selectRobot(null)}
            className="px-2 py-1 cursor-pointer"
            style={{ ...FONT.metric, color: COLORS.textSecondary, border: `1px solid ${COLORS.border}` }}
          >
            关闭
          </button>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3 flex flex-col gap-4">
          {/* 基本信息 */}
          <section>
            <h3 style={{ ...FONT.metric, color: COLORS.textSecondary }}>基本信息</h3>
            <div className="grid grid-cols-3 gap-2 mt-1">
              <div className="flex flex-col">
                <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>电量</span>
                <span className="tabular-nums" style={{ color: COLORS.textPrimary, fontWeight: 600 }}>
                  {robot ? `${robot.battery_pct.toFixed(1)}%` : '--'}
                </span>
              </div>
              <div className="flex flex-col">
                <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>最高关节温度</span>
                <span className="tabular-nums" style={{ color: COLORS.textPrimary, fontWeight: 600 }}>
                  {robot && robot.is_online === true ? `${robot.max_joint_temp.toFixed(1)}°C` : '--'}
                </span>
              </div>
              <div className="flex flex-col">
                {/*
                  「最后更新」优先用后端下发的 last_seen（**我们收到**那一帧的时刻），
                  而不是机器人自述的 ts：离线诊断时后者是机器人的说法（可能时钟漂移、
                  也可能是它自己算出来的），前者是坐在服务端这边的物证（ISSUE #77）。
                  两者都有时优先 last_seen；老后端只给 ts 时回落，不显示空白。
                */}
                <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>最后更新</span>
                <span className="tabular-nums" style={{ color: COLORS.textPrimary, fontWeight: 600 }}>
                  {lastSeenMs(robot) !== null
                    ? new Date(lastSeenMs(robot) as number).toLocaleTimeString('zh-CN', { hour12: false })
                    : '--'}
                </span>
              </div>
              <div className="flex flex-col">
                <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>状态码</span>
                <span className="tabular-nums" style={{ color: COLORS.textPrimary, fontWeight: 600 }}>
                  {robot ? robot.status_code : '--'}
                </span>
              </div>
              <div className="flex flex-col">
                <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>错误码</span>
                <span
                  className="tabular-nums"
                  style={{
                    color:
                      robot && robot.error_code !== 0 ? COLORS.statusFault : COLORS.textPrimary,
                    fontWeight: 600,
                  }}
                >
                  {robot ? robot.error_code : '--'}
                </span>
              </div>
              <div className="flex flex-col">
                <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>数据帧龄</span>
                <span className="tabular-nums" style={{ color: COLORS.textPrimary, fontWeight: 600 }}>
                  {frameAge(lastSeenMs(robot))}
                </span>
              </div>
            </div>
          </section>

          {/* 关节温度热力阵列（24 关节） */}
          <section>
            <h3 className="mb-1" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
              关节温度阵列（24 关节，spec/00 §5.5）
            </h3>
            <JointHeatmap
              temperatures={robot?.joint_temperatures ?? {}}
              selectedJoint={selectedJoint}
              onSelectJoint={setSelectedJoint}
            />
          </section>

          {/* 关节扭矩负载（实时帧快照，纯 CSS 条形——不新增 ECharts 实例以守住包体积。
              点击切换选中关节，与热力图联动；aria-label 以「扭矩 」开头，
              与热力图关节块的 aria-label（以关节名开头）在可访问性树上互不混淆）。 */}
          <section>
            <h3 className="mb-1" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
              关节扭矩负载（实时，点击切换选中关节）
            </h3>
            <div className="grid grid-cols-2 gap-x-3 gap-y-1 mt-1">
              {torqueRows.map(({ joint, value, pct }) => (
                <button
                  key={joint}
                  type="button"
                  aria-label={value === null ? `扭矩 ${joint} 无数据` : `扭矩 ${joint} ${value.toFixed(2)}N·m`}
                  onClick={() => setSelectedJoint(joint)}
                  className="flex items-center gap-1.5 min-w-0 cursor-pointer"
                  style={{
                    border: `1px solid ${joint === selectedJoint ? COLORS.accent : 'transparent'}`,
                    borderRadius: 2,
                    padding: '1px 2px',
                    backgroundColor: 'transparent',
                  }}
                >
                  <span
                    className="truncate shrink-0"
                    style={{ ...FONT.metric, color: COLORS.textSecondary, maxWidth: 104 }}
                  >
                    {joint}
                  </span>
                  <span
                    className="flex-1 h-1.5 min-w-0"
                    style={{ backgroundColor: COLORS.border, borderRadius: 1, overflow: 'hidden' }}
                  >
                    <span
                      className="block h-full"
                      style={{ width: `${pct}%`, backgroundColor: COLORS.accent }}
                    />
                  </span>
                  <span
                    className="tabular-nums shrink-0"
                    style={{ ...FONT.metric, color: COLORS.textPrimary }}
                  >
                    {value === null ? '--' : value.toFixed(2)}
                  </span>
                </button>
              ))}
            </div>
          </section>

          {/* 选中关节的历史折线（窗口可切：5/30/60m，切换经 queryKey 自动重拉） */}
          <section>
            <div className="flex items-center justify-between gap-2 mb-1">
              <h3 style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                {selectedJoint} · 最近 {historyMinutes} 分钟（{HISTORY_RESOLUTION} 分辨率）
              </h3>
              <div className="flex items-center gap-1 shrink-0">
                {HISTORY_WINDOW_OPTIONS.map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setHistoryMinutes(m)}
                    className="px-1.5 py-0.5 rounded cursor-pointer"
                    style={{
                      fontSize: '10px',
                      fontWeight: 600,
                      color: historyMinutes === m ? COLORS.bgBase : COLORS.textSecondary,
                      backgroundColor: historyMinutes === m ? COLORS.accent : 'transparent',
                      border: `1px solid ${historyMinutes === m ? COLORS.accent : COLORS.border}`,
                    }}
                  >
                    {m}m
                  </button>
                ))}
              </div>
            </div>
            {loading ? (
              <div className="h-56 flex items-center justify-center" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                历史数据加载中…
              </div>
            ) : error ? (
              <div className="h-56 flex items-center justify-center text-center px-4" style={{ ...FONT.metric, color: COLORS.statusFault }}>
                历史数据不可用（{error}）· {API_BASE}/robots/{selectedRobotId}/history
              </div>
            ) : chart.xAxis.length === 0 ? (
              <div className="h-56 flex items-center justify-center" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                最近 {historyMinutes} 分钟无历史点
              </div>
            ) : (
              <ReactEChartsCore
                echarts={echarts}
                option={chartOption}
                style={{ height: 224, width: '100%' }}
                notMerge
                lazyUpdate
              />
            )}
          </section>

          {/* 电量趋势（后端逐桶聚合值）。只在历史成功加载时挂载——
              空/错态文案只在上方关节图区块出现一次，避免测试与视觉的双重歧义。 */}
          {!loading && !error && chart.xAxis.length > 0 ? (
            <section>
              <h3 className="mb-1" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                电量趋势 · 最近 {historyMinutes} 分钟
              </h3>
              <ReactEChartsCore
                echarts={echarts}
                option={batteryChartOption}
                style={{ height: 112, width: '100%' }}
                notMerge
                lazyUpdate
              />
            </section>
          ) : null}

          {/* 该机器人最近告警（REST 权威视图，按 robot_id 过滤最近 200 条取前 5；
              订单 27 T2：admin 可就地确认/撤回，成功后 invalidate 重拉对账） */}
          <section>
            <h3 className="mb-1" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
              该机器人最近告警
            </h3>
            {ackError ? (
              <div className="mb-1" style={{ ...FONT.metric, color: COLORS.statusFault }}>
                {ackError}
              </div>
            ) : null}
            {alertsQuery.isPending && alertsQuery.fetchStatus !== 'idle' ? (
              <div style={{ ...FONT.metric, color: COLORS.textSecondary }}>告警加载中…</div>
            ) : alertsQuery.isError ? (
              // 刻意不带 HTTP 码：500 用例里 getByText(/HTTP 500/) 必须唯一命中历史图错误
              <div style={{ ...FONT.metric, color: COLORS.statusFault }}>告警加载失败</div>
            ) : robotAlerts.length === 0 ? (
              <div style={{ ...FONT.metric, color: COLORS.textSecondary }}>暂无告警记录</div>
            ) : (
              <ul className="flex flex-col gap-1.5">
                {robotAlerts.map((a) => (
                  <li key={a.alert_id} className="flex items-center gap-2 min-w-0">
                    <span
                      className="w-2 h-2 rounded-full shrink-0"
                      style={{ backgroundColor: severityColor(a.severity) }}
                    />
                    <span
                      className="tabular-nums shrink-0"
                      style={{ ...FONT.metric, color: COLORS.textSecondary }}
                    >
                      {new Date(a.time).toLocaleTimeString('zh-CN', { hour12: false })}
                    </span>
                    <span className="shrink-0" style={{ ...FONT.metric, color: severityColor(a.severity) }}>
                      {ruleLabel(a.rule_id)}
                    </span>
                    <span className="truncate" style={{ ...FONT.metric, color: COLORS.textPrimary }} title={a.message}>
                      {a.message}
                    </span>
                    {a.escalated ? (
                      <span className="shrink-0" style={{ ...FONT.metric, color: COLORS.statusWarning }}>
                        已升级
                      </span>
                    ) : null}
                    {a.suppressed ? (
                      // 订单 35：与 AlertLogTable 同一措辞、同一配色 token（textSecondary，
                      // 不是 statusFault —— 抑制不是一次新故障）。title 给根因规则，
                      // 让"为什么没弹出来"在本区就能回答。
                      <span
                        className="shrink-0"
                        style={{ ...FONT.metric, color: COLORS.textSecondary }}
                        title={
                          a.suppressed_by
                            ? `被更高等级告警抑制（根因规则：${ruleLabel(a.suppressed_by)}）`
                            : '被更高等级告警抑制'
                        }
                      >
                        ⊘ 已抑制
                      </span>
                    ) : null}
                    {a.ack ? (
                      <span className="shrink-0" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                        已确认
                      </span>
                    ) : null}
                    {canAck ? (
                      <button
                        type="button"
                        disabled={ackBusyId === a.alert_id}
                        onClick={() => void handleToggleAck(a)}
                        className="shrink-0 px-2 py-0.5 rounded cursor-pointer disabled:cursor-not-allowed disabled:opacity-40"
                        style={{
                          fontSize: '10px',
                          fontWeight: 600,
                          color: COLORS.textPrimary,
                          backgroundColor: COLORS.surface,
                          border: `1px solid ${COLORS.border}`,
                        }}
                        title={a.ack ? '撤回确认' : '确认告警'}
                      >
                        {a.ack ? '撤回' : '确认'}
                      </button>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </aside>
    </>
  );
}
