// spec/03 §5.5 —— 底部实时告警流（固定高 200px，新告警置顶）
// 订单 17 T4：加确认按钮（仅 admin）、已确认降噪、升级标记、筛选、分页。
// 订单 35 T3：被抑制项行内标「已抑制」（降视觉权重、**行不消失**）+
//             「显示已抑制」开关（默认关：抑制项不进未确认流）。
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
import { useI18n } from '../i18n/I18nProvider';

/** 每页条数。20 是"一屏可见 + 翻页不频繁"的折中（200px 高度可见约 6 行）。 */
export const ALERT_PAGE_SIZE = 20;

function formatTime(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** rule_id → i18n key (backend rules: motor_temp / battery_low / battery / offline / joint_stuck). */
export const RULE_KEY: Record<string, string> = {
  motor_temp: 'rule.motor_temp',
  battery_low: 'rule.battery_low',
  battery: 'rule.battery',
  offline: 'rule.offline',
  joint_stuck: 'rule.joint_stuck',
};

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
  /** 被抑制（订单 35）：只降视觉权重，不隐藏（可追溯是硬前提）。 */
  suppressed: boolean;
  /** 压制它的根因规则，如 offline / motor_temp；未抑制时为 null。 */
  suppressedBy: string | null;
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
    // WS 流里恒为 false/undefined —— 被抑制的告警**不会**被 PUBLISH（见 types/robot.ts）。
    // 写出来是为了让 Row 的来源映射对称，读的时候不必分情况。
    suppressed: a.suppressed ?? false,
    suppressedBy: a.suppressed_by ?? null,
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
    suppressed: a.suppressed,
    suppressedBy: a.suppressed_by,
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
  const { t } = useI18n();
  // 已确认 / 已抑制：降噪（灰度 + opacity），但**不隐藏** ——
  // 派工单禁止项 5（订单 17）与订单 35 的「行不得消失」，都是可追溯要求。
  // 两者共用一个 dimmed 表达：视觉上「不用现在处理」，语义由行内文字标签区分。
  const dimmed = row.ack || row.suppressed;
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
        title={`${row.robotId} · ${t(RULE_KEY[row.ruleId] ?? row.ruleId)} · ${row.message}`}
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
          {t(RULE_KEY[row.ruleId] ?? row.ruleId)} · S{row.severity}
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
            ⚠ {t('alert.escalated')}
          </span>
        )}
        {row.ack && (
          <span className="shrink-0" style={{ ...FONT.metric, color: COLORS.statusNormal }}>
            ✓ {t('alert.acked')}
          </span>
        )}
        {row.suppressed && (
          // 订单 35：被抑制行必须**在场**且可归因。title 带上根因规则，
          // 让「这条为什么没弹出来」在告警台内就能回答 —— 不必换到日志系统。
          // 用 textSecondary 而非 statusFault/Warning：抑制不是故障，
          // 是「根因已知，衍生症状无处置价值」，配色不该喊成一次新事故。
          <span
            className="shrink-0"
            style={{ ...FONT.metric, color: COLORS.textSecondary }}
            title={
              row.suppressedBy
                ? t('suppressed.title', { rule: t(RULE_KEY[row.suppressedBy] ?? row.suppressedBy) })
                : t('suppressed.titleSimple')
            }
          >
            ⊘ {t('alert.suppressed')}
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
          title={
            row.alertId === null
              ? t('ack.titleNoId')
              : row.ack
                ? t('ack.titleUnack')
                : t('ack.titleAck')
          }
        >
          {row.ack ? t('alert.unack') : t('alert.ack')}
        </button>
      )}
    </div>
  );
});

const FILTERS: { key: AlertFilter; labelKey: string }[] = [
  { key: 'all', labelKey: 'filter.all' },
  { key: 'unacked', labelKey: 'filter.unacked' },
  { key: 'escalated', labelKey: 'filter.escalated' },
];

export default function AlertLogTable() {
  const { t } = useI18n();
  const wsAlerts = useRobotStore((s) => s.alerts);
  const selectRobot = useRobotStore((s) => s.selectRobot);
  const applyAlertAck = useRobotStore((s) => s.applyAlertAck);
  const canAck = useIsAdmin();

  const [filter, setFilter] = useState<AlertFilter>('all');
  const [page, setPage] = useState(0);
  const [pageRows, setPageRows] = useState<Row[]>([]);
  const [total, setTotal] = useState(0);
  // 订单 35：抑制项默认**不进**未确认流（它们是"根因已知、暂时无处置价值"的行，
  // 混在待处理列表里只会掩盖真正要动手的那几条）。开关打开后才拉取。
  const [showSuppressed, setShowSuppressed] = useState(false);
  // 被抑制行的分页游标。刻意**独立**于 `page`：抑制行是少数，
  // 与主列表共用一个 offset 会让两边互相带偏页码（且 total 语义也不同）。
  const [suppressedPage, setSuppressedPage] = useState(0);
  const [suppressedRows, setSuppressedRows] = useState<Row[]>([]);
  const [suppressedTotal, setSuppressedTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [suppressedLoading, setSuppressedLoading] = useState(false);
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
        // `suppressed: false` —— 主列表显式排除被抑制行（订单 35）。
        // 不靠"后端默认排除"，因为契约刻意保持中立（见 router.list_alerts 的注释）；
        // 展示口径由本组件表达，别的地方（如 Drawer 的 200 条拉取）可以要全集。
        const res = await fetchAlerts({
          filter: f,
          limit: ALERT_PAGE_SIZE,
          offset: p * ALERT_PAGE_SIZE,
          suppressed: false,
        });
        setPageRows(res.items.map(fromApiItem));
        setTotal(res.total);
      } catch (e) {
        setError(e instanceof Error ? e.message : t('error.load'));
      } finally {
        setLoading(false);
      }
    },
    [t],
  );

  // 被抑制行单独一页：只在开关打开时拉（关着就不打这次 DB —— 默认视图零额外开销）。
  const loadSuppressed = useCallback(async (p: number) => {
    setSuppressedLoading(true);
    setError(null);
    try {
      const res = await fetchAlerts({
        filter: 'all',
        limit: ALERT_PAGE_SIZE,
        offset: p * ALERT_PAGE_SIZE,
        suppressed: true,
      });
      setSuppressedRows(res.items.map(fromApiItem));
      setSuppressedTotal(res.total);
    } catch (e) {
      setError(e instanceof Error ? e.message : t('error.load'));
    } finally {
      setSuppressedLoading(false);
    }
  }, [t]);

  // 首屏 + 筛选/翻页变化时重拉。WS 新告警不触发重拉（否则每来一条都打 DB）。
  useEffect(() => {
    void load(filter, page);
  }, [filter, page, load]);

  // 开关打开时拉被抑制页；关闭时保留已拉到的数据（再打开无需重拉，减少 DB 往返）。
  useEffect(() => {
    if (showSuppressed) void loadSuppressed(suppressedPage);
  }, [showSuppressed, suppressedPage, loadSuppressed]);

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
        setError(e instanceof Error ? e.message : t('error.op'));
      } finally {
        setBusyId(null);
      }
    },
    [applyAlertAck, t],
  );

  const totalPages = Math.max(1, Math.ceil(total / ALERT_PAGE_SIZE));
  const suppressedTotalPages = Math.max(1, Math.ceil(suppressedTotal / ALERT_PAGE_SIZE));

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
          <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>{t('alert.header')}</span>
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
                {t(f.labelKey)}
              </button>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-3">
          {error && <span style={{ ...FONT.metric, color: COLORS.statusFault }}>{error}</span>}
          {/* 订单 35：抑制项的显示开关。刻意做成**方框**而非常驻筛选 tab ——
              它表达的是"要不要看见被压住的行"，与「看哪一类」的 tab 是正交维度，
              用同一个视觉语言会让人以为它与「全部/未确认/已升级」互斥。 */}
          <button
            type="button"
            role="switch"
            aria-checked={showSuppressed}
            onClick={() => {
              setShowSuppressed((v) => !v);
              setSuppressedPage(0);
            }}
            className="px-2 py-0.5 rounded cursor-pointer"
            style={{
              fontSize: '10px',
              fontWeight: 600,
              color: showSuppressed ? COLORS.bgBase : COLORS.textSecondary,
              backgroundColor: showSuppressed ? COLORS.accent : 'transparent',
              border: `1px solid ${showSuppressed ? COLORS.accent : COLORS.border}`,
            }}
          >
            {showSuppressed ? t('filter.showSuppressedOn') : t('filter.showSuppressed')}
          </button>
          <span className="tabular-nums" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
            {t('pager.total', { total, page: page + 1, pages: totalPages })}
          </span>
          <div className="flex items-center gap-1">
            <button
              type="button"
              disabled={page === 0 || loading}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              className="px-2 py-0.5 rounded cursor-pointer disabled:cursor-not-allowed disabled:opacity-40"
              style={{ fontSize: '10px', color: COLORS.textPrimary, border: `1px solid ${COLORS.border}` }}
            >
              {t('pager.prev')}
            </button>
            <button
              type="button"
              disabled={page + 1 >= totalPages || loading}
              onClick={() => setPage((p) => p + 1)}
              className="px-2 py-0.5 rounded cursor-pointer disabled:cursor-not-allowed disabled:opacity-40"
              style={{ fontSize: '10px', color: COLORS.textPrimary, border: `1px solid ${COLORS.border}` }}
            >
              {t('pager.next')}
            </button>
          </div>
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto">
        {rows.length === 0 && !showSuppressed ? (
          <div
            className="h-full flex items-center justify-center"
            style={{ ...FONT.metric, color: COLORS.textSecondary }}
          >
            {loading ? t('empty.loading') : t('empty.noAlerts')}
          </div>
        ) : (
          <>
            {rows.map((row) => (
              <AlertRow
                key={row.id}
                row={row}
                canAck={canAck}
                busy={busyId === row.alertId}
                onOpen={selectRobot}
                onToggleAck={handleToggleAck}
              />
            ))}
            {/* 被抑制区：与主行流**同一张表**（同一 AlertRow 组件、同一条代码路径），
                只在末尾加一条分隔说明。不做成第二个表格/弹层 —— 抑制项与正常项
                除了标记外形态完全一致，分开渲染只会让两处样式各自漂移。 */}
            {showSuppressed && (
              <>
                <div
                  className="px-3 py-0.5 flex items-center justify-between gap-2"
                  style={{
                    ...FONT.metric,
                    color: COLORS.textSecondary,
                    backgroundColor: COLORS.bgBase,
                    borderTop: `1px solid ${COLORS.border}`,
                    borderBottom: `1px solid ${COLORS.border}`,
                  }}
                >
                  <span>{t('suppressed.header', { n: suppressedTotal })}</span>
                  <span className="tabular-nums">
                    <button
                      type="button"
                      disabled={suppressedPage === 0 || suppressedLoading}
                      onClick={() => setSuppressedPage((p) => Math.max(0, p - 1))}
                      className="px-2 rounded cursor-pointer disabled:cursor-not-allowed disabled:opacity-40"
                      style={{ fontSize: '10px', color: COLORS.textPrimary, border: `1px solid ${COLORS.border}` }}
                    >
                      {t('pager.prev')}
                    </button>{' '}
                    <span>
                      {suppressedPage + 1}/{suppressedTotalPages}
                    </span>{' '}
                    <button
                      type="button"
                      disabled={suppressedPage + 1 >= suppressedTotalPages || suppressedLoading}
                      onClick={() => setSuppressedPage((p) => p + 1)}
                      className="px-2 rounded cursor-pointer disabled:cursor-not-allowed disabled:opacity-40"
                      style={{ fontSize: '10px', color: COLORS.textPrimary, border: `1px solid ${COLORS.border}` }}
                    >
                      {t('pager.next')}
                    </button>
                  </span>
                </div>
                {suppressedRows.length === 0 ? (
                  <div className="px-3 py-1" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                    {suppressedLoading ? t('empty.loading') : t('suppressed.empty')}
                  </div>
                ) : (
                  suppressedRows.map((row) => (
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
              </>
            )}
          </>
        )}
      </div>
    </section>
  );
}
