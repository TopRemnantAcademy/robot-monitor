// FxBackdrop — animated cyber background layer (aurora + grid).
//
// Renders a theme-aware solid base plus two decorative layers (aurora blobs and a
// perspective grid). Sits behind content (z-index 0, pointer-events none, hidden
// from the a11y tree). Animations are defined in theme/motion.css and are
// disabled under prefers-reduced-motion.

import { COLORS } from '../theme/tokens';

interface FxBackdropProps {
  /** 'page' = stronger effect for the full-screen landing; 'panel' = subtle for the dashboard. */
  variant?: 'page' | 'panel';
}

export default function FxBackdrop({ variant = 'panel' }: FxBackdropProps) {
  return (
    <div className="fx-aurora fx-grid" aria-hidden="true" style={{ backgroundColor: COLORS.bgBase }}>
      <div
        className={variant === 'page' ? 'fx-aurora--page' : undefined}
        style={{ position: 'absolute', inset: 0 }}
      />
    </div>
  );
}
