// spec/03 §5.2 —— 顶栏三 KPI（在线率 / 当前告警数 / 平均电量）+ WS 连接状态点

import { useMemo } from 'react';
import { COLORS, FONT } from '../theme/tokens';
import { selectStatusCounts, useRobotStore } from '../store/robotStore';
import { useShallow } from 'zustand/react/shallow';
import { useI18n } from '../i18n/I18nProvider';
import LanguageSwitcher from '../i18n/LanguageSwitcher';

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
  const { t } = useI18n();
  const alerts = useRobotStore((s) => s.alerts);
  const wsStatus = useRobotStore((s) => s.wsStatus);

  // ISSUE #75 结构性共享：不再订阅整张 robots Map（每个 100ms 刷库帧都会让
  // 顶栏重渲染一次）。改为 useShallow 包装的**派生统计选择器** —— 输出按字段
  // 浅比较，只有「屏幕上真正显示的值」变化时才重渲染：
  //   - onlineRate/avgBattery 四舍五入到 1 位小数（与 .toFixed(1) 显示精度一致，
  //     否则 57.03→57.06 这类纯浮点抖动也会穿透浅比较触发无意义重渲染）；
  //   - online/total/stale 本身就是低频整数；
  //   - alerts / wsStatus 保持各自单字段订阅（低频，且 useShallow 对数组/字符串
  //     无收益）。
  // selectStatusCounts 接收 { robots } 而非闭包 Map —— 保持与原实现同一取数路径。
  const stats = useRobotStore(
    useShallow((s) => {
      const counts = selectStatusCounts(s);
      let batterySum = 0;
      let batteryCount = 0;
      // ISSUE #77：在线数与平均电量**从后端下发的 is_online 来**，
      // 不再用 `status_code !== 0` 自己数 —— 后者把停报机器人永久算作在线。
      for (const r of s.robots.values()) {
        if (r.is_online === true) {
          batterySum += r.battery_pct;
          batteryCount += 1;
        }
      }
      // 在线率 = 在线数 / 100（spec/03 §5.2），离线/失联机器人不计入平均电量
      return {
        onlineRate: counts.total === 0 ? 0 : Math.round((counts.online / counts.total) * 1000) / 10,
        online: counts.online,
        total: counts.total,
        stale: counts.stale,
        avgBattery: batteryCount === 0 ? 0 : Math.round((batterySum / batteryCount) * 10) / 10,
      };
    }),
  );

  // 当前告警数 = severity ≥ 2 的告警（spec/03 §5.2；未 ack 语义由前端流式计算）
  const activeAlerts = useMemo(() => alerts.filter((a) => a.severity >= 2).length, [alerts]);

  const dotColor =
    wsStatus === 'open' ? COLORS.statusNormal : wsStatus === 'connecting' ? COLORS.statusWarning : COLORS.statusFault;
  const dotLabel =
    wsStatus === 'open' ? t('ws.connected') : wsStatus === 'connecting' ? t('ws.connecting') : t('ws.reconnecting');

  return (
    <header
      className="h-14 shrink-0 flex items-center justify-between px-4"
      style={{ backgroundColor: COLORS.surface, borderBottom: `1px solid ${COLORS.border}` }}
    >
      <div className="flex items-baseline gap-2">
        <span style={{ ...FONT.title, color: COLORS.textPrimary }}>RobotMonitor</span>
        <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>
          {t('header.subtitle')}
        </span>
      </div>

      <div className="flex items-center gap-6">
        <Kpi
          label={t('kpi.onlineRate')}
          value={stats.onlineRate.toFixed(1)}
          // 失联台数直接挂在括号里：只看百分比看不出"是有 1 台还是 40 台掉了"，
          // 而这两者的处置动作完全不同（前者去现场，后者先查 Redis/MQTT 链路）。
          unit={`% (${stats.online}/${stats.total}${stats.stale > 0 ? t('unit.lost', { n: stats.stale }) : ''})`}
          color={stats.onlineRate >= 90 ? COLORS.statusNormal : COLORS.statusWarning}
        />
        <Kpi
          label={t('kpi.alerts')}
          value={String(activeAlerts)}
          color={activeAlerts > 0 ? COLORS.statusFault : COLORS.textPrimary}
        />
        <Kpi label={t('kpi.avgBattery')} value={stats.avgBattery.toFixed(1)} unit="%" />

        <LanguageSwitcher />

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
