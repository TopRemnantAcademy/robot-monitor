// spec/03 §5.2 —— 顶栏三 KPI（在线率 / 当前告警数 / 平均电量）+ WS 连接状态点

import { useMemo } from 'react';
import { COLORS, FONT } from '../theme/tokens';
import { useRobotStore } from '../store/robotStore';

function Kpi({ label, value, unit, color }: { label: string; value: string; unit?: string; color?: string }) {
  return (
    <div className="flex flex-col items-end min-w-[110px]">
      <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>{label}</span>
      <span className="tabular-nums" style={{ fontSize: '18px', fontWeight: 700, color: color ?? COLORS.textPrimary }}>
        {value}
        {unit ? <span style={{ ...FONT.metric, marginLeft: 2 }}>{unit}</span> : null}
      </span>
    </div>
  );
}

export default function HealthSummaryHeader() {
  const robots = useRobotStore((s) => s.robots);
  const alerts = useRobotStore((s) => s.alerts);
  const wsStatus = useRobotStore((s) => s.wsStatus);

  const stats = useMemo(() => {
    let online = 0;
    let batterySum = 0;
    let batteryCount = 0;
    for (const r of robots.values()) {
      if (r.status_code !== 0) {
        online += 1;
        batterySum += r.battery_pct;
        batteryCount += 1;
      }
    }
    const total = robots.size;
    // 在线率 = 在线数 / 100（spec/03 §5.2），离线机器人不计入平均电量
    return {
      onlineRate: total === 0 ? 0 : (online / total) * 100,
      online,
      total,
      avgBattery: batteryCount === 0 ? 0 : batterySum / batteryCount,
    };
  }, [robots]);

  // 当前告警数 = severity ≥ 2 的告警（spec/03 §5.2；未 ack 语义由前端流式计算）
  const activeAlerts = useMemo(() => alerts.filter((a) => a.severity >= 2).length, [alerts]);

  const dotColor =
    wsStatus === 'open' ? COLORS.statusNormal : wsStatus === 'connecting' ? COLORS.statusWarning : COLORS.statusFault;
  const dotLabel = wsStatus === 'open' ? '已连接' : wsStatus === 'connecting' ? '连接中' : '断开重连中';

  return (
    <header
      className="h-14 shrink-0 flex items-center justify-between px-4"
      style={{ backgroundColor: COLORS.surface, borderBottom: `1px solid ${COLORS.border}` }}
    >
      <div className="flex items-baseline gap-2">
        <span style={{ ...FONT.title, color: COLORS.textPrimary }}>RobotMonitor</span>
        <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>
          人形机器人工厂 · 100 台实时监控
        </span>
      </div>

      <div className="flex items-center gap-6">
        <Kpi
          label="在线率"
          value={stats.onlineRate.toFixed(1)}
          unit={`% (${stats.online}/${stats.total})`}
          color={stats.onlineRate >= 90 ? COLORS.statusNormal : COLORS.statusWarning}
        />
        <Kpi
          label="当前告警"
          value={String(activeAlerts)}
          color={activeAlerts > 0 ? COLORS.statusFault : COLORS.textPrimary}
        />
        <Kpi label="平均电量" value={stats.avgBattery.toFixed(1)} unit="%" />

        <div className="flex items-center gap-2">
          <span
            className="inline-block w-2.5 h-2.5 rounded-full"
            style={{ backgroundColor: dotColor, boxShadow: `0 0 6px ${dotColor}` }}
          />
          <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>WS {dotLabel}</span>
        </div>
      </div>
    </header>
  );
}
