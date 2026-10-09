// spec/03 §5.3 —— 单机关节热力诊断 Drawer（右滑 480px，不挤压主区）
//
// 数据来源：
//   1) 实时增量：直接从 Zustand 的 robots Map 取（该机器人 10Hz 更新）
//   2) 历史折线：GET {VITE_API_BASE}/robots/{id}/history?minutes=5&resolution=10s（spec/00 §5.3）

import { useEffect, useMemo, useRef, useState } from 'react';
import ReactECharts from 'echarts-for-react';
import JointHeatmap from './JointHeatmap';
import { COLORS, FONT, STATUS_COLOR, STATUS_LABEL } from '../theme/tokens';
import { useRobotStore } from '../store/robotStore';
import { JOINT_NAMES } from '../types/robot';
import type { HistoryPoint, RobotHistoryResponse } from '../types/robot';

/** REST 基址：Vite 只暴露 VITE_ 前缀（orders/05 禁止项） */
export const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

const HISTORY_MINUTES = 5;
const HISTORY_RESOLUTION = '10s';

interface ChartSeries {
  name: string;
  data: (number | null)[];
}

interface ChartModel {
  xAxis: string[];
  series: ChartSeries[];
}

export default function RobotDetailDrawer() {
  const selectedRobotId = useRobotStore((s) => s.selectedRobotId);
  const robots = useRobotStore((s) => s.robots);
  const selectRobot = useRobotStore((s) => s.selectRobot);

  const [selectedJoint, setSelectedJoint] = useState<string>(JOINT_NAMES[0]);
  const [history, setHistory] = useState<HistoryPoint[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 每次打开 Drawer 重置选中关节，避免残留上一台机器人的选择
  useEffect(() => {
    if (selectedRobotId) {
      setSelectedJoint(JOINT_NAMES[0]);
      setHistory([]);
      setError(null);
    }
  }, [selectedRobotId]);

  // 拉取最近 5 分钟历史（10s 分辨率）
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => {
    if (!selectedRobotId) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const url = `${API_BASE}/robots/${encodeURIComponent(selectedRobotId)}/history?minutes=${HISTORY_MINUTES}&resolution=${HISTORY_RESOLUTION}`;
    setLoading(true);
    setError(null);

    fetch(url, { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<RobotHistoryResponse>;
      })
      .then((payload) => setHistory(Array.isArray(payload.points) ? payload.points : []))
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        setError(err instanceof Error ? err.message : 'history 请求失败');
      })
      .finally(() => setLoading(false));

    return () => controller.abort();
  }, [selectedRobotId]);

  const robot = selectedRobotId ? robots.get(selectedRobotId) : undefined;

  // ECharts 折线模型：温度/扭矩/角度 三条 series，x 轴为 10s 桶时间
  const chart = useMemo<ChartModel>(() => {
    const xAxis: string[] = [];
    const temp: (number | null)[] = [];
    const torque: (number | null)[] = [];
    const position: (number | null)[] = [];

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
    }
    return {
      xAxis,
      series: [
        { name: '温度(°C)', data: temp },
        { name: '扭矩(N·m)', data: torque },
        { name: '角度(rad)', data: position },
      ],
    };
  }, [history, selectedJoint]);

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
      lineStyle: { width: 2, color: [COLORS.statusFault, COLORS.accent, COLORS.statusNormal][idx] },
      itemStyle: { color: [COLORS.statusFault, COLORS.accent, COLORS.statusNormal][idx] },
    })),
    animation: false,
  };

  const statusColor = robot ? STATUS_COLOR[robot.status_code] : COLORS.statusOffline;

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
            <span style={{ ...FONT.metric, color: statusColor }}>
              {robot ? STATUS_LABEL[robot.status_code] : '无数据'}
            </span>
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
                  {robot && robot.status_code !== 0 ? `${robot.max_joint_temp.toFixed(1)}°C` : '--'}
                </span>
              </div>
              <div className="flex flex-col">
                <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>最后更新</span>
                <span className="tabular-nums" style={{ color: COLORS.textPrimary, fontWeight: 600 }}>
                  {robot && robot.ts > 0
                    ? new Date(robot.ts).toLocaleTimeString('zh-CN', { hour12: false })
                    : '--'}
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

          {/* 选中关节的 5 分钟折线 */}
          <section>
            <h3 className="mb-1" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
              {selectedJoint} · 最近 {HISTORY_MINUTES} 分钟（{HISTORY_RESOLUTION} 分辨率）
            </h3>
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
                最近 {HISTORY_MINUTES} 分钟无历史点
              </div>
            ) : (
              <ReactECharts option={chartOption} style={{ height: 224, width: '100%' }} notMerge lazyUpdate />
            )}
          </section>
        </div>
      </aside>
    </>
  );
}
