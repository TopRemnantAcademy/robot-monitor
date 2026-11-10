// spec/03 §4 layout — three-zone grid, overall h-screen flex flex-col overflow-hidden (no page-level scrolling)
//
// Order 15: wrap the outermost layer with LoginGate. When not logged in, **do not render** any dashboard component,
// so no WS connection is established and no REST request is sent — an unauthorized user cannot even see "what the dashboard looks like".
//
// Order 26 (I-20): wrap App with ErrorBoundary (render-failure fallback, no more white screen) +
// QueryClientProvider (server-side state caching/retry; zustand continues to manage WS client state, complementing each other).

import { useEffect, useState } from 'react';
import { ErrorBoundary } from 'react-error-boundary';
import { QueryClientProvider } from '@tanstack/react-query';
import HealthSummaryHeader from './components/HealthSummaryHeader';
import FleetOverviewCards from './components/FleetOverviewCards';
import FleetHealthAnalytics from './components/FleetHealthAnalytics';
import SupportedRobotsPanel from './components/SupportedRobotsPanel';
import AlertLogTable from './components/AlertLogTable';
import RobotDetailDrawer from './components/RobotDetailDrawer';
import LoginGate from './auth/LoginGate';
import { setUnauthorizedHandler } from './auth/api';
import { useAuthStore } from './auth/authStore';
import { useRobotWebSocket } from './hooks/useRobotWebSocket';
import { startObservabilityReport } from './lib/observability';
import { COLORS } from './theme/tokens';
import { makeQueryClient } from './lib/queryClient';
import ErrorFallback from './ErrorFallback';

/** Dashboard body shown after login. WS is only established here — not when unauthenticated. */
function Dashboard() {
  // Single global WS entry point: 100ms throttled store flush (spec/03 §6)
  useRobotWebSocket();

  // Order 16 T2: print one re-render summary every 5s (wsMsgs / storeCommits / cardRenders / ratio).
  // Mounted on Dashboard (not App) deliberately: when unauthenticated, LoginGate does not render Dashboard,
  // so it won't start a timer that only prints all-zeros and pollutes the console.
  useEffect(() => startObservabilityReport(), []);

  return (
    <div
      className="h-screen w-screen flex flex-col overflow-hidden"
      style={{ backgroundColor: COLORS.bgBase, color: COLORS.textPrimary }}
    >
      <HealthSummaryHeader />
      {/* UI enhancement: fleet-level distribution + trend layer (see FleetHealthAnalytics top comment).
          fixed-height strip (shrink-0), card wall flex-1 auto-shrink, page still no scroll. */}
      <FleetHealthAnalytics />
      <SupportedRobotsPanel />
      <FleetOverviewCards />
      <AlertLogTable />
      <RobotDetailDrawer />
    </div>
  );
}

export default function App() {
  const bootstrap = useAuthStore((s) => s.bootstrap);

  // QueryClient is built only once within App's lifecycle: lazy init via useState,
  // to avoid rebuilding on every render (which would clear the cache). React 18 StrictMode double-invoke also keeps just one.
  const [queryClient] = useState(makeQueryClient);

  useEffect(() => {
    // Unified 401 exit: any business request receiving 401 clears token and returns to login.
    // Registered at the api layer rather than at each call site, so newly added requests aren't missed.
    setUnauthorizedHandler(() => useAuthStore.getState().markAnonymous());
    void bootstrap();
    return () => setUnauthorizedHandler(null);
  }, [bootstrap]);

  return (
    // ErrorBoundary is outside the Provider: even the Provider's own exceptions are caught.
    <ErrorBoundary FallbackComponent={ErrorFallback}>
      <QueryClientProvider client={queryClient}>
        <LoginGate>
          <Dashboard />
        </LoginGate>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
