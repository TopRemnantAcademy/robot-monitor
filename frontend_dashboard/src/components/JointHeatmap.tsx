// spec/03 §5.4 joint temperature color-block array (inside Drawer)
// Strictly renders by the 24 standard key names from spec/00 §5.5 (order is JOINT_NAMES order, left arm → right arm → left leg → right leg).

import { COLORS, tempColor } from '../theme/tokens';
import { JOINT_NAMES } from '../types/robot';
import type { JointDict } from '../types/robot';
import { useI18n } from '../i18n/I18nProvider';

interface JointHeatmapProps {
  temperatures: JointDict;
  /** Highlight outline for the selected joint (linked with the ECharts line chart) */
  selectedJoint?: string | null;
  onSelectJoint?: (joint: string) => void;
}

/**
 * Joint → two-line short name. At 480px Drawer / 6 columns each block is ~66px,
 * a single-line full name (e.g. "L-shoulder pitch") is inevitably truncated to "L-shoulde…",
 * so split into "part" + "axis" two lines to keep it readable (spec/03 §5.4 requires showing joint short names).
 * Example: left_shoulder_pitch → L·shoulder / pitch
 */
function shortName(joint: string): { group: string; axis: string } {
  const side = joint.startsWith('left_') ? 'L' : joint.startsWith('right_') ? 'R' : '?';
  const body = joint.replace(/^(left|right)_/, '');
  // Axis suffix: pitch / roll / yaw
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
        // When the backend didn't report this joint, value is undefined, the block shows "--", avoiding a fake 0°C green block
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
              // When temperature color is the background, use dark text to ensure contrast (colors still strictly from tokens)
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
