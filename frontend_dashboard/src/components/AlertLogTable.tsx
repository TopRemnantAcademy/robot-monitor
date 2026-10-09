// spec/03 §5.5 —— 底部实时告警流（固定高 200px，新告警置顶）
// 数据源：WS type:"alert"（后端订阅 Redis robot:alerts 转发，spec/00 §5.2 ③）

import { useMemo } from 'react';
import { COLORS, FONT, severityColor } from '../theme/tokens';
import { useRobotStore } from '../store/robotStore';
import type { RobotAlert } from '../types/robot';

function formatTime(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** rule_id → 展示标签（后端规则：motor_temp / battery / offline / joint_stuck） */
const RULE_LABEL: Record<string, string> = {
  motor_temp: '电机过热',
  battery: '低电量',
  offline: '离线',
  joint_stuck: '关节卡死',
};

function ruleLabel(ruleId: string): string {
  return RULE_LABEL[ruleId] ?? ruleId;
}

export default function AlertLogTable() {
  const alerts = useRobotStore((s) => s.alerts);
  const selectRobot = useRobotStore((s) => s.selectRobot);

  // severity 降序不必要——后端已按时间推送，store 里是 prepend 顺序（新→旧）
  const rows = useMemo(() => alerts.slice(0, 60), [alerts]);

  return (
    <section
      className="h-[200px] shrink-0 flex flex-col"
      style={{ backgroundColor: COLORS.surface, borderTop: `1px solid ${COLORS.border}` }}
    >
      <div className="h-7 shrink-0 flex items-center justify-between px-3" style={{ borderBottom: `1px solid ${COLORS.border}` }}>
        <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>
          实时告警流 · 最近 {rows.length} 条（severity ≥ 3 红色 / = 2 黄色）
        </span>
        <span className="tabular-nums" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
          累计 {alerts.length}
        </span>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto">
        {rows.length === 0 ? (
          <div className="h-full flex items-center justify-center" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
            暂无告警
          </div>
        ) : (
          rows.map((alert: RobotAlert, idx: number) => (
            <button
              key={`${alert.ts}-${alert.robot_id}-${alert.rule_id}-${idx}`}
              type="button"
              onClick={() => selectRobot(alert.robot_id)}
              className="w-full flex items-center gap-3 px-3 py-1 text-left cursor-pointer"
              style={{ borderBottom: `1px solid ${COLORS.border}` }}
            >
              <span className="tabular-nums shrink-0" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                {formatTime(alert.ts)}
              </span>
              <span className="tabular-nums shrink-0" style={{ ...FONT.metric, color: COLORS.textPrimary, fontWeight: 600, width: 88 }}>
                {alert.robot_id}
              </span>
              <span
                className="shrink-0 px-1.5 py-0.5 rounded"
                style={{
                  fontSize: '10px',
                  fontWeight: 600,
                  color: COLORS.bgBase,
                  backgroundColor: severityColor(alert.severity),
                }}
              >
                {ruleLabel(alert.rule_id)} · S{alert.severity}
              </span>
              <span className="truncate" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                {alert.message}
              </span>
            </button>
          ))
        )}
      </div>
    </section>
  );
}
