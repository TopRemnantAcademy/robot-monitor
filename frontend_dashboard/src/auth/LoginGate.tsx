// Login gate (Order 15 T4)
//
// Renders in three states based on authStore.status:
//   unknown        → minimal placeholder (first screen probing token, avoid login-page flash)
//   anonymous      → the public landing page (LandingPage): a normal homepage with the sign-in
//                    control in the top-right corner and a lazily-loaded registration form.
//   authenticated  → normal dashboard
//
// Styling reuses design tokens (spec/03 §3), no new color values introduced. The 401→login
// handling lives in App (setUnauthorizedHandler → authStore.markAnonymous), not here.

import { useAuthStore } from './authStore';
import LandingPage from './LandingPage';
import { COLORS } from '../theme/tokens';

export default function LoginGate({ children }: { children: React.ReactNode }) {
  const status = useAuthStore((s) => s.status);

  if (status === 'unknown') {
    return (
      <div
        className="h-screen w-screen flex items-center justify-center"
        style={{ backgroundColor: COLORS.bgBase, color: COLORS.textSecondary }}
      >
        Checking login status…
      </div>
    );
  }

  if (status === 'anonymous') return <LandingPage />;

  return <>{children}</>;
}
