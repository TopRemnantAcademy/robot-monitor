// spec/03 §5.5 —— 底部实时告警流（固定高 200px，新告警置顶）
// 数据源：WS type:"alert"（后端订阅 Redis robot:alerts 转发，spec/00 §5.2 ③）
//
// 【性能：告警是「无常」的，其余 UI 应静】
// 本组件原先每次新告警到达都会整表重算 60 行按钮（每帧新建 60 个闭包与 DOM 节点），
// 而告警是低频事件（1000 msg/s 的遥测流里只有零星几条），不该由它驱动主区重排。
// 这里拆成「表头固定区 + 行 memo 组件」：新告警进来只有新增那一行挂载，
// 已有 59 行的 props 未变 → memo 命中 → 不重渲染（R-01 §6 性能护栏）。
//
// 渲染上限与 store 缓冲上限解耦：store 保留 200 条供统计口径使用，
// 表格只渲染可见的前 60 条（200px 高度屏幕外本就不可见）。

import { memo, useMemo } from 'react';
import { COLORS, FONT, severityColor } from '../theme/tokens';
import { useRobotStore } from '../store/robotStore';
import type { RobotAlert } from '../types/robot';

/** 表格实际渲染上限（与 store 的 ALERT_BUFFER_LIMIT=200 职责不同，勿混淆） */
export const ALERT_RENDER_LIMIT = 60;

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

interface AlertRowProps {
  alert: RobotAlert;
  /** 稳定回调：父组件用 useCallback 固定引用，否则 memo 失效 */
  onOpen: (robotId: string) => void;
}

const AlertRow = memo(function AlertRow({ alert, onOpen }: AlertRowProps) {
  return (
    <button
      type="button"
      onClick={() => onOpen(alert.robot_id)}
      className="w-full flex items-center gap-3 px-3 py-0.5 text-left cursor-pointer whitespace-nowrap"
      style={{ borderBottom: `1px solid ${COLORS.border}` }}
      title={`${alert.robot_id} · ${ruleLabel(alert.rule_id)} · ${alert.message}`}
    >
      <span className="tabular-nums shrink-0" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
        {formatTime(alert.ts)}
      </span>
      {/* robot_id 是 "humanoid-067" 共 12 字符，11px/600 实测约 76px；
          固定 88px 曾被 flex 压缩成两行（humanoid- / 067），故改为不收缩的 min-w。 */}
      <span
        className="tabular-nums shrink-0"
        style={{ ...FONT.metric, color: COLORS.textPrimary, fontWeight: 600, minWidth: 82 }}
      >
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
  );
});

export default function AlertLogTable() {
  const alerts = useRobotStore((s) => s.alerts);
  const selectRobot = useRobotStore((s) => s.selectRobot);

  // store 里的 alerts 每次都是新数组，但行内容（alert 对象）是稳定的 ——
  // memo 比较的是每行的 alert 引用，因此这里无需再 memo 一层切片结果。
  const rows = useMemo(() => alerts.slice(0, ALERT_RENDER_LIMIT), [alerts]);

  return (
    <section
      className="h-[200px] shrink-0 flex flex-col"
      style={{ backgroundColor: COLORS.surface, borderTop: `1px solid ${COLORS.border}` }}
    >
      <div
        className="h-7 shrink-0 flex items-center justify-between px-3"
        style={{ borderBottom: `1px solid ${COLORS.border}` }}
      >
        <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>
          实时告警流 · 显示最近 {rows.length} 条（severity ≥ 3 红色 / = 2 黄色）
        </span>
        <span className="tabular-nums" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
          累计 {alerts.length}
        </span>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto">
        {rows.length === 0 ? (
          <div
            className="h-full flex items-center justify-center"
            style={{ ...FONT.metric, color: COLORS.textSecondary }}
          >
            暂无告警
          </div>
        ) : (
          rows.map((alert) => (
            // key 必须只用告警自身的稳定字段。prepend 语义下若带上 idx，
            // 每次新增一行都会让所有行 key 平移 → React 全部重挂 → memo 永不命中。
            // 同一毫秒内同机器人同规则理论上可能重复，但那时内容也相同，React key 重复
            // 仅告警一次且渲染结果可见（不会丢行），比整表重建的代价小得多。
            <AlertRow
              key={`${alert.ts}-${alert.robot_id}-${alert.rule_id}`}
              alert={alert}
              onOpen={selectRobot}
            />
          ))
        )}
      </div>
    </section>
  );
}
