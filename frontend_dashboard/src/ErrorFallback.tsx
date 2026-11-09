// 订单 26（I-20）：全局错误边界的兜底 UI。
//
// 定位：**最后一道防线**。组件渲染抛错时，整棵子树被卸载，用户看到这块而不是白屏。
// 大屏是"无人值守"场景 —— 残缺点亮好过全黑，所以给两个动作：
//   1) 「重试」resetErrorBoundary()：先把边界内的子树重挂一遍（多数瞬时错误可恢复）；
//   2) 「刷新页面」：边界外的状态（如 WS）也重置，兜底更彻底。
// 样式沿用 design token（spec/03 §3），不引入新色值。

import type { FallbackProps } from 'react-error-boundary';
import { COLORS, FONT } from './theme/tokens';
import { useI18n } from './i18n/I18nProvider';

export default function ErrorFallback({ error, resetErrorBoundary }: FallbackProps) {
  const { t } = useI18n();
  const message = error instanceof Error ? error.message : String(error);

  return (
    <div
      className="h-screen w-screen flex flex-col items-center justify-center gap-4 px-6 text-center"
      style={{ backgroundColor: COLORS.bgBase, color: COLORS.textPrimary }}
      role="alert"
    >
      <h1 style={{ ...FONT.title, color: COLORS.statusFault }}>{t('error.title')}</h1>
      <p style={{ ...FONT.metric, color: COLORS.textSecondary, maxWidth: 640 }}>
        {t('error.desc')}
      </p>
      <pre
        className="overflow-auto px-3 py-2"
        style={{
          ...FONT.metric,
          color: COLORS.textSecondary,
          backgroundColor: COLORS.surface,
          border: `1px solid ${COLORS.border}`,
          maxWidth: 720,
          maxHeight: 160,
          whiteSpace: 'pre-wrap',
          textAlign: 'left',
        }}
      >
        {message}
      </pre>
      <div className="flex gap-3">
        <button
          type="button"
          onClick={resetErrorBoundary}
          className="px-3 py-1 cursor-pointer"
          style={{ ...FONT.metric, color: COLORS.textPrimary, border: `1px solid ${COLORS.border}` }}
        >
          {t('error.retry')}
        </button>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="px-3 py-1 cursor-pointer"
          style={{ ...FONT.metric, color: COLORS.textPrimary, border: `1px solid ${COLORS.border}` }}
        >
          {t('error.reload')}
        </button>
      </div>
    </div>
  );
}
