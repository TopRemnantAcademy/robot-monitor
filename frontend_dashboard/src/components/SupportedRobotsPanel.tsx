// 平台支持的机型目录（UI + admin UI 角色感知）。
//
// - viewer：机型卡片（名称 / 形态 / 自由度 / 在册数 / 在线数 / 说明）。
// - admin ：同卡片 + 机群管理明细（厂商 / 固件版本 / 工位 / 未登记标记）。
//
// 数据来自 GET /supported-robots（DB 驱动：robot_metadata.model_type 去重 + 机群构成）。
// 颜色 / 字号全部走 theme/tokens，不引入新色相（spec/03 §3）。
//
// 布局：本组件是 `shrink-0` 横条（固定高度、内部横向滚动），插在
// FleetHealthAnalytics 与 FleetOverviewCards 之间，不挤占卡片墙的 flex-1 空间。

import { useQuery } from '@tanstack/react-query';
import { COLORS, FONT, RADIUS } from '../theme/tokens';
import { fetchSupportedRobots } from '../auth/api';
import { useIsAdmin } from '../auth/authStore';
import { supportedRobotsKey, type SupportedRobot } from '../types/robot';

export default function SupportedRobotsPanel() {
  const isAdmin = useIsAdmin();
  const { data, isLoading, isError } = useQuery({
    queryKey: supportedRobotsKey,
    queryFn: fetchSupportedRobots,
    staleTime: 60_000,
  });

  const headerRight = data
    ? `${data.models.length} 款 · 在册 ${data.total_robots} 台`
    : isLoading
      ? '加载中…'
      : '—';

  return (
    <section
      className="shrink-0 px-3 py-2"
      style={{ backgroundColor: COLORS.bgBase, borderBottom: `1px solid ${COLORS.border}` }}
    >
      <div className="flex items-center gap-2 mb-1.5">
        <span style={{ ...FONT.title, fontSize: '14px', color: COLORS.textPrimary }}>
          平台支持的机型
        </span>
        <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>{headerRight}</span>
        {isAdmin && (
          <span
            className="ml-auto px-1.5 py-0.5 rounded-sm"
            style={{
              fontSize: '10px',
              color: COLORS.accent,
              border: `1px solid ${COLORS.accent}`,
            }}
          >
            管理视图
          </span>
        )}
      </div>

      {isError ? (
        <span style={{ ...FONT.metric, color: COLORS.statusFault }}>机型目录加载失败</span>
      ) : (
        <div className="flex gap-2 overflow-x-auto pb-1">
          {(data?.models ?? []).map((m) => (
            <ModelCard key={m.model_type} model={m} isAdmin={isAdmin} />
          ))}
          {isLoading && !data && (
            <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>加载中…</span>
          )}
        </div>
      )}
    </section>
  );
}

function ModelCard({ model, isAdmin }: { model: SupportedRobot; isAdmin: boolean }) {
  const allOnline = model.total > 0 && model.online === model.total;
  const onlineColor = allOnline ? COLORS.statusNormal : COLORS.statusWarning;

  return (
    <div
      className="shrink-0 flex flex-col gap-1 px-3 py-2"
      style={{
        minWidth: '208px',
        backgroundColor: COLORS.surface,
        border: `1px solid ${COLORS.border}`,
        borderRadius: RADIUS.card,
      }}
    >
      <div className="flex items-center justify-between gap-2">
        <span style={{ fontSize: '13px', fontWeight: 700, color: COLORS.textPrimary }}>
          {model.display_name}
        </span>
        {model.generic && (
          <span style={{ fontSize: '9px', color: COLORS.textSecondary }}>未登记</span>
        )}
      </div>

      <span style={{ fontSize: '10px', color: COLORS.textSecondary }}>{model.model_type}</span>

      <div className="flex flex-wrap gap-1">
        <Tag label={model.form_factor === '—' ? '形态未定义' : model.form_factor} />
        <Tag label={model.dof > 0 ? `${model.dof} 自由度` : '自由度未定义'} />
      </div>

      <div className="flex items-center gap-2" style={{ fontSize: '11px' }}>
        <span style={{ color: COLORS.textSecondary }}>在册</span>
        <span className="tabular-nums" style={{ color: COLORS.textPrimary }}>
          {model.total}
        </span>
        <span style={{ color: COLORS.textSecondary }}>在线</span>
        <span className="tabular-nums" style={{ color: onlineColor }}>
          {model.online}
        </span>
      </div>

      {isAdmin && (
        <div
          className="flex flex-col gap-0.5 mt-0.5 pt-1"
          style={{
            borderTop: `1px solid ${COLORS.border}`,
            fontSize: '10px',
            color: COLORS.textSecondary,
          }}
        >
          <span>厂商：{model.vendor}</span>
          <span>
            固件：
            {model.firmware_versions.length ? model.firmware_versions.join(' / ') : '—'}
          </span>
          <span>工位：{model.stations} 个</span>
        </div>
      )}

      <span
        style={{ fontSize: '10px', color: COLORS.textSecondary, lineHeight: 1.4 }}
      >
        {model.description}
      </span>
    </div>
  );
}

function Tag({ label }: { label: string }) {
  return (
    <span
      className="px-1.5 py-0.5 rounded-sm"
      style={{
        fontSize: '10px',
        color: COLORS.textSecondary,
        backgroundColor: COLORS.bgBase,
        border: `1px solid ${COLORS.border}`,
      }}
    >
      {label}
    </span>
  );
}
