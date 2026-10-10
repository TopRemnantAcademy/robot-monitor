// spec/03 §5.5 —— 底部实时告警流（固定高 200px，新告警置顶）
// 订单 17 T4：加确认按钮（仅 admin）、已确认降噪、升级标记、筛选、分页。
//
// 【两个数据源，职责不同 —— 这是本组件最重要的设计点】
//   - WS `type:"alert"`：**增量**新告警的实时推送（spec/00 §5.2 ③）。只含"刚发生"，
//     且 ack/escalated 这类**事后变化**不会推给你。
//   - `GET /alerts`：**权威分页视图**。含 ack/escalated 的当前值、支持筛选与分页。
// 因此本表以 REST 为权威（决定"有哪些行、每行 ack 状态"），WS 只负责把最新一条
// prepend 进来，让值班员不用手动刷新就能看到新告警。筛选/翻页时重拉 REST。
//
// 【性能：告警是低频事件，其余 UI 应静】
// 行组件 memo：新告警到达时只有新增那一行挂载，已有行 props 未变 → 不重渲染。
// 确认按钮的回调用 useCallback 固定引用，否则 memo 恒失效（同 R-01 §6 护栏）。

import { memo, useCallback, useEffect, useState } from 'react';
import { COLORS, FONT, severityColor } from '../theme/tokens';
import { useRobotStore } from '../store/robotStore';
import { useIsAdmin } from '../auth/authStore';
import { ackAlert, fetchAlerts, unackAlert } from '../auth/alertsApi';
import type { AlertApiItem, AlertFilter, RobotAlert } from '../types/robot';

/** 每页条数。20 是"一屏可见 + 翻页不频繁"的折中（200px 高度可见约 6 行）。 */
export const ALERT_PAGE_SIZE = 20;

function formatTime(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** rule_id → 展示标签（后端规则：motor_temp / battery_low / offline / joint_stuck） */
const RULE_LABEL: Record<string, string> = {
  motor_temp: '电机过热',
  battery_low: '低电量',
  battery: '低电量',
  offline: '离线',
  joint_stuck: '关节卡死',
};

function ruleLabel(ruleId: string): string {
  return RULE_LABEL[ruleId] ?? ruleId;
}

/** 统一行模型：WS 实时条目与 REST 条目都归一到这里再渲染。 */
interface Row {
  id: string;
  alertId: number | null;
  robotId: string;
  ruleId: string;
  severity: number;
  message: string;
  ts: number;
  ack: boolean;
  escalated: boolean;
}

function fromWsAlert(a: RobotAlert): Row {
  return {
    id: `${a.ts}-${a.robot_id}-${a.rule_id}`,
    alertId: a.alert_id ?? null,
    robotId: a.robot_id,
    ruleId: a.rule_id,
    severity: a.severity,
    message: a.message,
    ts: a.ts,
    ack: a.ack ?? false,
    escalated: a.escalated ?? false,
  };
}

function fromApiItem(a: AlertApiItem): Row {
  return {
    id: `db-${a.alert_id}`,
    alertId: a.alert_id,
    robotId: a.robot_id,
    ruleId: a.rule_id,
    severity: a.severity,
    message: a.message,
    ts: new Date(a.time).getTime(),
    ack: a.ack,
    escalated: a.escalated,
  };
}

interface AlertRowProps {
  row: Row;
  canAck: boolean;
  busy: boolean;
  onOpen: (robotId: string) => void;
  onToggleAck: (row: Row) => void;
}

const AlertRow = memo(function AlertRow({ row, canAck, busy, onOpen, onToggleAck }: AlertRowProps) {
  // 已确认：降噪（灰度 + opacity），但**不隐藏** —— 派工单禁止项 5，可追溯。
  const dimmed = row.ack;
  return (
    <div
      className="w-full flex items-center gap-3 px-3 py-0.5 whitespace-nowrap"
      style={{
        borderBottom: `1px solid ${COLORS.border}`,
        // 升级告警左侧加一条醒目竖条（不改背景，避免整行抢眼到看不清文字）
        boxShadow: row.escalated ? `inset 3px 0 0 ${COLORS.statusWarning}` : undefined,
        opacity: dimmed ? 0.45 : 1,
        filter: dimmed ? 'grayscale(1)' : undefined,
      }}
    >
      <button
        type="button"
        onClick={() => onOpen(row.robotId)}
        className="flex-1 min-w-0 flex items-center gap-3 text-left cursor-pointer"
        title={`${row.robotId} · ${ruleLabel(row.ruleId)} · ${row.message}`}
      >
        <span className="tabular-nums shrink-0" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
          {formatTime(row.ts)}
        </span>
        <span
          className="tabular-nums shrink-0"
          style={{ ...FONT.metric, color: COLORS.textPrimary, fontWeight: 600, minWidth: 82 }}
        >
          {row.robotId}
        </span>
        <span
          className="shrink-0 px-1.5 py-0.5 rounded"
          style={{
            fontSize: '10px',
            fontWeight: 600,
            color: COLORS.bgBase,
            backgroundColor: severityColor(row.severity),
          }}
        >
          {ruleLabel(row.ruleId)} · S{row.severity}
        </span>
        {row.escalated && (
          <span
            className="shrink-0 px-1.5 py-0.5 rounded"
            style={{
              fontSize: '10px',
              fontWeight: 700,
              color: COLORS.bgBase,
              backgroundColor: COLORS.statusWarning,
            }}
          >
            ⚠ 已升级
          </span>
        )}
        {row.ack && (
          <span className="shrink-0" style={{ ...FONT.metric, color: COLORS.statusNormal }}>
            ✓ 已确认
          </span>
        )}
        <span className="truncate" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
          {row.message}
        </span>
      </button>

      {canAck && (
        <button
          type="button"
          disabled={row.alertId === null || busy}
          onClick={() => onToggleAck(row)}
          className="shrink-0 px-2 py-0.5 rounded cursor-pointer disabled:cursor-not-allowed"
          style={{
            fontSize: '10px',
            fontWeight: 600,
            color: COLORS.textPrimary,
            backgroundColor: COLORS.surface,
            border: `1px solid ${COLORS.border}`,
            opacity: row.alertId === null ? 0.4 : 1,
          }}
          title={row.alertId === null ? '该告警无 ID，无法确认' : row.ack ? '撤回确认' : '确认告警'}
        >
          {row.ack ? '撤回' : '确认'}
        </button>
      )}
    </div>
  );
});

const FILTERS: { key: AlertFilter; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'unacked', label: '未确认' },
  { key: 'escalated', label: '已升级' },
];

export default function AlertLogTable() {
  const wsAlerts = useRobotStore((s) => s.alerts);
  const selectRobot = useRobotStore((s) => s.selectRobot);
  const applyAlertAck = useRobotStore((s) => s.applyAlertAck);
  const canAck = useIsAdmin();

  const [filter, setFilter] = useState<AlertFilter>('all');
  const [page, setPage] = useState(0);
  const [pageRows, setPageRows] = useState<Row[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  // 已本地确认/撤回的告警 ID → 最新状态。REST 重拉后用它对账，
  // 避免"刚确认完翻页回来又变未确认"（后端其实已改，只是拉取有延迟）。
  const [localAck, setLocalAck] = useState<Record<number, boolean>>({});

  const load = useCallback(
    async (f: AlertFilter, p: number) => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetchAlerts({ filter: f, limit: ALERT_PAGE_SIZE, offset: p * ALERT_PAGE_SIZE });
        setPageRows(res.items.map(fromApiItem));
        setTotal(res.total);
      } catch (e) {
        setError(e instanceof Error ? e.message : '加载失败');
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  // 首屏 + 筛选/翻页变化时重拉。WS 新告警不触发重拉（否则每来一条都打 DB）。
  useEffect(() => {
    void load(filter, page);
  }, [filter, page, load]);

  // 把 REST 行与本地 ack 状态、以及 WS 最新告警合并。
  // WS 只在第 0 页且"全部"筛选下 prepend —— 翻页/筛选时插新行会让分页错位。
  const rows: Row[] = (() => {
    const base = pageRows.map((r) => {
      if (r.alertId !== null && r.alertId in localAck && localAck[r.alertId] !== r.ack) {
        return { ...r, ack: localAck[r.alertId] };
      }
      return r;
    });
    if (page !== 0 || filter !== 'all') return base;
    // WS 流里「未在 REST 页中出现」的新告警（用 robot+ts+rule 去重）
    const seen = new Set(base.map((r) => `${r.robotId}-${r.ts}-${r.ruleId}`));
    const extra = wsAlerts
      .map(fromWsAlert)
      .filter((r) => !seen.has(`${r.robotId}-${r.ts}-${r.ruleId}`));
    return [...extra, ...base].slice(0, ALERT_PAGE_SIZE);
  })();

  const handleToggleAck = useCallback(
    async (row: Row) => {
      if (row.alertId === null) return;
      const id = row.alertId;
      setBusyId(id);
      try {
        const res = row.ack ? await unackAlert(id) : await ackAlert(id);
        setLocalAck((prev) => ({ ...prev, [id]: res.ack }));
        // 同步更新 WS 流里的那条（若存在），让列表立即可见变化。
        applyAlertAck(id, { ack: res.ack, ack_at: res.ack_at, ack_by: res.ack_by });
        // 幂等命中（changed=false）时不报错：状态已是目标值，UI 无忧。
      } catch (e) {
        setError(e instanceof Error ? e.message : '操作失败');
      } finally {
        setBusyId(null);
      }
    },
    [applyAlertAck],
  );

  const totalPages = Math.max(1, Math.ceil(total / ALERT_PAGE_SIZE));

  return (
    <section
      className="h-[200px] shrink-0 flex flex-col"
      style={{ backgroundColor: COLORS.surface, borderTop: `1px solid ${COLORS.border}` }}
    >
      <div
        className="h-7 shrink-0 flex items-center justify-between px-3 gap-3"
        style={{ borderBottom: `1px solid ${COLORS.border}` }}
      >
        <div className="flex items-center gap-3">
          <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>实时告警流</span>
          <div className="flex items-center gap-1">
            {FILTERS.map((f) => (
              <button
                key={f.key}
                type="button"
                onClick={() => {
                  setFilter(f.key);
                  setPage(0);
                }}
                className="px-2 py-0.5 rounded cursor-pointer"
                style={{
                  fontSize: '10px',
                  fontWeight: 600,
                  color: filter === f.key ? COLORS.bgBase : COLORS.textSecondary,
                  backgroundColor: filter === f.key ? COLORS.accent : 'transparent',
                  border: `1px solid ${filter === f.key ? COLORS.accent : COLORS.border}`,
                }}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-3">
          {error && <span style={{ ...FONT.metric, color: COLORS.statusFault }}>{error}</span>}
          <span className="tabular-nums" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
            共 {total} 条 · 第 {page + 1}/{totalPages} 页
          </span>
          <div className="flex items-center gap-1">
            <button
              type="button"
              disabled={page === 0 || loading}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              className="px-2 py-0.5 rounded cursor-pointer disabled:cursor-not-allowed disabled:opacity-40"
              style={{ fontSize: '10px', color: COLORS.textPrimary, border: `1px solid ${COLORS.border}` }}
            >
              上一页
            </button>
            <button
              type="button"
              disabled={page + 1 >= totalPages || loading}
              onClick={() => setPage((p) => p + 1)}
              className="px-2 py-0.5 rounded cursor-pointer disabled:cursor-not-allowed disabled:opacity-40"
              style={{ fontSize: '10px', color: COLORS.textPrimary, border: `1px solid ${COLORS.border}` }}
            >
              下一页
            </button>
          </div>
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto">
        {rows.length === 0 ? (
          <div
            className="h-full flex items-center justify-center"
            style={{ ...FONT.metric, color: COLORS.textSecondary }}
          >
            {loading ? '加载中…' : '暂无告警'}
          </div>
        ) : (
          rows.map((row) => (
            <AlertRow
              key={row.id}
              row={row}
              canAck={canAck}
              busy={busyId === row.alertId}
              onOpen={selectRobot}
              onToggleAck={handleToggleAck}
            />
          ))
        )}
      </div>
    </section>
  );
}
