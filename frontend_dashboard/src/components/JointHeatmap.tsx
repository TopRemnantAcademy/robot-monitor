// spec/03 §5.4 关节温度色块阵列（Drawer 内）
// 严格按 spec/00 §5.5 的 24 个标准键名渲染（顺序即 JOINT_NAMES 顺序，左臂→右臂→左腿→右腿）。

import { COLORS, tempColor } from '../theme/tokens';
import { JOINT_NAMES } from '../types/robot';
import type { JointDict } from '../types/robot';
import { useI18n } from '../i18n/I18nProvider';

interface JointHeatmapProps {
  temperatures: JointDict;
  /** 选中的关节高亮描边（与 ECharts 折线联动） */
  selectedJoint?: string | null;
  onSelectJoint?: (joint: string) => void;
}

/**
 * 关节 → 两行短名。480px Drawer / 6 列下每块约 66px，
 * 单行完整名（如 "L-shoulder pitch"）必然被截断成 "L-shoulde…"，
 * 故拆成「部位」+「轴向」两行，保证可读（spec/03 §5.4 要求显示关节短名）。
 * 例：left_shoulder_pitch → L·shoulder / pitch
 */
function shortName(joint: string): { group: string; axis: string } {
  const side = joint.startsWith('left_') ? 'L' : joint.startsWith('right_') ? 'R' : '?';
  const body = joint.replace(/^(left|right)_/, '');
  // 轴向后缀：pitch / roll / yaw
  const m = body.match(/^(.*)_(pitch|roll|yaw)$/);
  const group = m ? m[1] : body;
  const axis = m ? m[2] : '';
  return { group: `${side}·${group}`, axis };
}

export default function JointHeatmap({ temperatures, selectedJoint, onSelectJoint }: JointHeatmapProps) {
  const { t } = useI18n();
  return (
    <div className="grid grid-cols-6 gap-1">
      {JOINT_NAMES.map((joint) => {
        // 后端未上报该关节时给 undefined，色块显示 "--"，避免伪造 0℃ 绿块
        const value = temperatures?.[joint];
        const hasValue = typeof value === 'number' && Number.isFinite(value);
        const bg = hasValue ? tempColor(value as number) : COLORS.statusOffline;
        const selected = selectedJoint === joint;
        const name = shortName(joint);
        return (
          <button
            key={joint}
            type="button"
            title={`${joint}${hasValue ? ` · ${(value as number).toFixed(1)}°C` : ` · ${t('joint.noData')}`}`}
            aria-label={`${joint} ${hasValue ? `${(value as number).toFixed(1)}°C` : t('joint.noData')}`}
            onClick={() => onSelectJoint?.(joint)}
            className="h-10 rounded flex flex-col items-center justify-center px-1 cursor-pointer transition-colors"
            style={{
              backgroundColor: bg,
              border: selected ? `2px solid ${COLORS.accent}` : `1px solid ${COLORS.border}`,
              borderRadius: '6px',
              // 温度色作为底色时用深色字，保证对比度（配色仍严格取自 token）
            }}
          >
            <span
              className="w-full text-center leading-tight truncate"
              style={{ fontSize: '9px', color: COLORS.bgBase, fontWeight: 600 }}
            >
              {name.group}
            </span>
            <span
              className="tabular-nums leading-none"
              style={{ fontSize: '11px', color: COLORS.bgBase, fontWeight: 700 }}
            >
              {hasValue ? `${(value as number).toFixed(1)}` : '--'}
              <span style={{ fontSize: '8px', fontWeight: 600 }}> {name.axis || '°C'}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}
