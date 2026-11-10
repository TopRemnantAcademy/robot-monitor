// spec/03 §5.5 —— bottom real-time alert stream (fixed height 200px, newest alerts on top)
// Order 17 T4: added acknowledge button (admin only), acknowledged-noise reduction, escalation marker, filtering, pagination.
// Order 35 T3: suppressed items marked inline "Suppressed" (reduced visual weight, **row does not disappear**) +
//             "Show suppressed" toggle (default off: suppressed items don't enter the unacknowledged stream).
//
// NOTE — Two data sources with different responsibilities (this is the most important design point of this component):
//   - WS `type:"alert"`: **incremental** real-time push of new alerts (spec/00 §5.2 ③). Only contains "just happened",
//     and post-hoc changes like ack/escalated are NOT pushed to you.
//   - `GET /alerts`: **authoritative paginated view**. Contains current values of ack/escalated, supports filtering and pagination.
// So this table treats REST as authoritative (decides "which rows exist, each row's ack state"), WS only prepends the latest one,
// so the operator can see new alerts without manually refreshing. Re-fetch REST on filter/page change.
//
// NOTE — Performance: alerts are low-frequency events, the rest of the UI should stay quiet:
// Row component memo: when a new alert arrives, only the new row mounts, existing rows' props unchanged → no re-render.
// The ack button's callback uses useCallback to fix the reference, otherwise memo is always defeated (same R-01 §6 guardrail).

import { memo, useCallback, useEffect, useState } from 'react';
import { COLORS, FONT, severityColor } from '../theme/tokens';
import { useRobotStore } from '../store/robotStore';
import { useIsAdmin } from '../auth/authStore';
import { ackAlert, fetchAlerts, unackAlert } from '../auth/alertsApi';
import type { AlertApiItem, AlertFilter, RobotAlert } from '../types/robot';
import { useI18n } from '../i18n/I18nProvider';

/** Items per page. 20 is a compromise of "visible on one screen + infrequent paging" (≈6 rows visible at 200px height). */
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

/** Unified row model: both WS real-time items and REST items are normalized here before rendering. */
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
  /** Suppressed (Order 35): only reduce visual weight, don't hide (traceability is a hard requirement). */
  suppressed: boolean;
  /** The root-cause rule that suppresses it, e.g. offline / motor_temp; null when not suppressed. */
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
    // In the WS stream this is always false/undefined —— suppressed alerts are **not** PUBLISHed (see types/robot.ts).
    // Written out to make the Row's source mapping symmetric, so you don't need to branch when reading.
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
  // Acknowledged / suppressed: reduce noise (grayscale + opacity), but **don't hide** ——
  // work-order prohibition #5 (Order 17) and Order 35's "rows must not disappear" are both traceability requirements.
  // Both share one dimmed expression: visually "no need to handle now", semantics distinguished by inline text labels.
  const dimmed = row.ack || row.suppressed;
  return (
    <div
      className="w-full flex items-center gap-3 px-3 py-0.5 whitespace-nowrap"
      style={{
        borderBottom: `1px solid ${COLORS.border}`,
        // Escalated alert gets a prominent vertical bar on the left (doesn't change background, to avoid the whole row being so eye-catching you can't read the text)
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
          // Order 35: suppressed rows must be **present** and attributable. The title carries the root-cause rule,
          // so "why didn't this pop up" can be answered within the alert console —— no need to switch to the log system.
          // Use textSecondary rather than statusFault/Warning: suppression is not a fault,
          // it's "root cause known, derivative symptom has no handling value", the color shouldn't scream like a new incident.
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
  // Order 35: suppressed items are **not** in the unacknowledged stream by default (they are rows where "root cause is known, temporarily no handling value",
  // mixed into the to-do list they'd only mask the few that actually need action). Only fetched after the toggle is turned on.
  const [showSuppressed, setShowSuppressed] = useState(false);
  // The pagination cursor for suppressed rows. Deliberately **independent** from `page`: suppressed rows are few,
  // sharing one offset with the main list would skew each other's page numbers (and the total semantics also differ).
  const [suppressedPage, setSuppressedPage] = useState(0);
  const [suppressedRows, setSuppressedRows] = useState<Row[]>([]);
  const [suppressedTotal, setSuppressedTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [suppressedLoading, setSuppressedLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  // Locally acked/withdrawn alert IDs → their latest state. Reconciled against this after a REST re-fetch,
  // so a just-acked alert doesn't flip back to unacked when paging back (backend already changed, just fetch lag).
  const [localAck, setLocalAck] = useState<Record<number, boolean>>({});

  const load = useCallback(
    async (f: AlertFilter, p: number) => {
      setLoading(true);
      setError(null);
      try {
        // `suppressed: false` —— the main list explicitly excludes suppressed rows (Order 35).
        // Not relying on "backend excludes by default", because the contract deliberately stays neutral (see router.list_alerts comment);
        // the display contract is expressed by this component; other places (e.g. the Drawer's 200-item fetch) may want the full set.
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

  // Suppressed rows get their own page: only fetched when the toggle is on (when off, don't hit the DB —— zero extra cost for the default view).
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

  // Re-fetch on first screen + filter/page change. WS new alerts don't trigger re-fetch (otherwise every incoming one would hit the DB).
  useEffect(() => {
    void load(filter, page);
  }, [filter, page, load]);

  // Fetch the suppressed page when the toggle is on; when off, keep the already-fetched data (re-opening needs no re-fetch, reducing DB round-trips).
  useEffect(() => {
    if (showSuppressed) void loadSuppressed(suppressedPage);
  }, [showSuppressed, suppressedPage, loadSuppressed]);

  // Merge REST rows with local ack state, and the latest WS alerts.
  // WS only prepends on page 0 with the "all" filter —— inserting new rows during paging/filtering would misalign pagination.
  const rows: Row[] = (() => {
    const base = pageRows.map((r) => {
      if (r.alertId !== null && r.alertId in localAck && localAck[r.alertId] !== r.ack) {
        return { ...r, ack: localAck[r.alertId] };
      }
      return r;
    });
    if (page !== 0 || filter !== 'all') return base;
    // New alerts in the WS stream that are "not in the REST page" (dedupe by robot+ts+rule)
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
        // Sync-update the matching one in the WS stream (if present), so the list reflects the change immediately.
        applyAlertAck(id, { ack: res.ack, ack_at: res.ack_at, ack_by: res.ack_by });
        // On idempotent hit (changed=false) don't error: the state is already the target value, UI is fine.
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
          {/* Order 35: the display toggle for suppressed items. Deliberately made a **checkbox** rather than a persistent filter tab ——
              it expresses "whether to see the suppressed rows", orthogonal to the "which category" tabs,
              using the same visual language would make people think it's mutually exclusive with "all/unacknowledged/escalated". */}
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
            {/* Suppressed zone: the **same table** as the main row flow (same AlertRow component, same code path),
                only adds a separator note at the end. Not made into a second table/popover —— suppressed items and normal items
                are identical in form except for the marker; separate rendering would only let the two styles drift apart. */}
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
