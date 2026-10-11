// spec/03 §5.2 —— 顶栏三 KPI（在线率 / 当前告警数 / 平均电量）+ WS 连接状态点

import { useMemo } from 'react';
import { COLORS, FONT } from '../theme/tokens';
import { selectStatusCounts, useRobotStore } from '../store/robotStore';

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
    // ISSUE #77：在线数与「当前告警」一样，**从后端下发的 is_online 来**，
    // 不再用 `status_code !== 0` 自己数 —— 后者把停报机器人永久算作在线，
    // 于是顶栏的「在线率 100.0%」在整条产线断电时依然绿得理直气壮。
    const counts = selectStatusCounts({ robots });
    let batterySum = 0;
    let batteryCount = 0;
    for (const r of robots.values()) {
      if (r.is_online === true) {
        batterySum += r.battery_pct;
        batteryCount += 1;
      }
    }
    // 在线率 = 在线数 / 100（spec/03 §5.2），离线/失联机器人不计入平均电量
    return {
      onlineRate: counts.total === 0 ? 0 : (counts.online / counts.total) * 100,
      online: counts.online,
      total: counts.total,
      stale: counts.stale,
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
          // 失联台数直接挂在括号里：只看百分比看不出"是有 1 台还是 40 台掉了"，
          // 而这两者的处置动作完全不同（前者去现场，后者先查 Redis/MQTT 链路）。
          unit={`% (${stats.online}/${stats.total}${stats.stale > 0 ? ` · 失联 ${stats.stale}` : ''})`}
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
